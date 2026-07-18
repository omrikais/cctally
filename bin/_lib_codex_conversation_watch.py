"""Pure directory-frontier kernel for Codex live-tail child discovery (spec §5.4).

Layout-agnostic, budget-bounded, incremental discovery of brand-new Codex
rollout files that no table yet knows — the second of the two live-tail child-
discovery layers (the first being the driver's DB re-resolve of the watched
conversation's file set). This module has NO I/O of its own: the filesystem
``scandir`` / directory-``mtime`` / file-``size`` reads and the clock are
injected as callables, so the whole cycle is unit-testable without a real tree,
real timing, or a DB. The thin SSE driver in
``bin/_cctally_dashboard_conversation.py`` owns the DB re-resolve, the targeted
ingest of the paths this kernel surfaces, and the child classification that
feeds ``reap``; this kernel owns only the directory frontier, the pending-
candidate set, and the shared per-cycle operation budget.

All six §5.4 bullets are contract:

1. **walk_root-only seed.** The frontier index starts as JUST the configured
   ``CodexProviderRoot.walk_root`` — construction walks nothing. Directories are
   discovered incrementally as the frontier is enumerated.
2. **One shared per-cycle operation budget** covering directory ``stat``s,
   directory enumerations, and pending-candidate ``stat``s, with round-robin
   fairness across the three work classes and rotating continuation cursors, so
   a large tree or a large candidate population can neither starve the frontier
   nor blow the cycle's ceiling. Every known directory / candidate is
   *eventually* visited — not all of them every cycle.
3. **Re-enumeration on own-mtime change** or new frontier membership — never on
   an ancestor's mtime (POSIX/APFS bumps only the immediate parent's mtime on
   entry creation, so ancestor-mtime pruning would be unsound).
4. **Symlink boundary.** Descendant directory/file entries are traversed with
   ``follow_symlinks=False`` semantics: a symlinked entry is NEVER traversed, so
   every directory the frontier reaches is connected to ``walk_root`` by a chain
   of physical (non-symlink) parent links and therefore stays physically inside
   the canonical ``walk_root`` — the realpath-containment guarantee, achieved
   structurally. The configured ``walk_root`` spelling itself is preserved (it is
   the seed and is never realpath-rewritten), matching discovery's configured-
   spelling-vs-physical distinction.
5. **Rotation watermark pinned to rotation-start.** A "rotation" is one full pass
   of the stat cursor over the frontier. The completed-rotation watermark
   advances only when a rotation completes and is pinned to that rotation's
   START time, so an entry created mid-rotation (which changes its own
   directory's mtime) is re-examined on the next rotation.
6. **Pending candidates.** Every newly discovered file joins ``pending`` BEFORE
   its first ingest attempt (so a discovered file is never lost even if its
   first ingest is dirty/lock-contended and leaves no cursor row). A path is
   retried while unclassified: with no committed cursor it is surfaced every
   time it is visited; with a committed cursor it is surfaced only when its size
   exceeds that cursor (an incomplete-``session_meta`` tail completing without
   any directory-mtime change). A path leaves the set when the driver ``reap``s
   it (it gained a thread row — child or non-child), when it vanishes, or on a
   bounded per-candidate expiry (handed to the next full sync).
"""
from __future__ import annotations

import os


# ── production-default injected callables ─────────────────────────────────────


def default_scandir(dirpath):
    """Yield ``(name, path, is_dir, is_symlink)`` for the entries of ``dirpath``.

    ``is_dir`` uses ``follow_symlinks=False`` (a symlink-to-dir reads as NOT a
    dir), and ``is_symlink`` flags any symlink so the frontier can refuse to
    traverse it. Returns ``[]`` on any ``OSError`` (vanished / permission)."""
    out = []
    try:
        with os.scandir(dirpath) as it:
            for de in it:
                try:
                    is_sym = de.is_symlink()
                    is_dir = de.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                out.append((de.name, de.path, is_dir, is_sym))
    except OSError:
        return []
    return out


def default_dir_mtime(dirpath):
    """The directory's own mtime (``st_mtime_ns``), or ``None`` if it can't be
    stat'd. Follows a symlinked ``walk_root`` (the seed is intentionally the
    configured spelling)."""
    try:
        return os.stat(dirpath).st_mtime_ns
    except OSError:
        return None


def default_file_size(filepath):
    """Regular-file size in bytes, or ``None`` if the path is gone or not a
    regular file (size-only, matching sync_codex_cache's append-only delta)."""
    try:
        st = os.stat(filepath)
    except OSError:
        return None
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        return None
    return st.st_size


_SENTINEL = object()


class CodexChildFrontier:
    """The budgeted directory frontier + pending-candidate set for one watched
    Codex conversation's root (spec §5.4). See the module docstring for the
    contract. ``cycle`` is the only per-tick entry point; ``reap`` is called by
    the driver after a targeted ingest classifies discovered candidates."""

    def __init__(
        self,
        walk_root,
        *,
        op_budget: int = 64,
        pending_expiry_cycles: int = 50,
        scandir_fn=default_scandir,
        dir_mtime_fn=default_dir_mtime,
        file_size_fn=default_file_size,
        clock_fn=None,
    ):
        self._walk_root = str(walk_root)
        self._op_budget = max(1, int(op_budget))
        self._pending_expiry = max(1, int(pending_expiry_cycles))
        self._scandir = scandir_fn
        self._dir_mtime = dir_mtime_fn
        self._file_size = file_size_fn
        self._clock = clock_fn or (lambda: 0)

        # Frontier index — seeded with JUST walk_root (no enumeration at start).
        self._dirs: list[str] = [self._walk_root]
        self._dir_set: set[str] = {self._walk_root}
        # dir -> mtime observed when it was last enumerated (absent => never).
        self._enum_mtime: dict[str, int] = {}
        # dirs awaiting a scandir, each carried with the mtime that triggered it.
        self._enum_queue: list[tuple[str, int]] = []

        # Pending candidates: path -> age (cycles since registration).
        self._pending: dict[str, int] = {}

        # Rotating continuation cursors (persist across cycles).
        self._stat_cursor = 0
        self._pending_rr = 0                 # where the pending sweep resumes
        self._rotation_start = None
        self._completed_rotation_watermark = None
        self._rotation_count = 0

    # ── introspection (tests / diagnostics) ──────────────────────────────────

    def known_directories(self) -> set[str]:
        return set(self._dir_set)

    def pending_candidates(self) -> set[str]:
        return set(self._pending)

    @property
    def rotation_watermark(self):
        return self._completed_rotation_watermark

    # ── driver feedback ───────────────────────────────────────────────────────

    def reap(self, classified_paths) -> None:
        """Drop pending candidates the driver has classified (they gained a
        thread row — child or non-child). A child is now in the watched file set
        and tailed by the main loop; a non-child needs no further attention."""
        for p in classified_paths:
            self._pending.pop(p, None)

    # ── one discovery cycle ───────────────────────────────────────────────────

    def cycle(self, *, known_paths, committed_sizes) -> set[str]:
        """One budgeted discovery cycle (§5.4).

        ``known_paths`` — every path already tracked in ``codex_session_files``,
        so a newly enumerated file already known to some table is not
        re-registered. ``committed_sizes`` — ``{path: size_bytes}`` committed
        cursors, for the pending-growth check.

        Returns the set of candidate paths to targeted-ingest this cycle (new
        discoveries whose first ingest hasn't landed + pending retries that grew
        or remain unclassified). Bounded to ``op_budget`` filesystem operations,
        round-robin fair across directory stats, enumerations, and pending
        checks."""
        self._known = set(known_paths)
        self._committed = committed_sizes or {}
        ingest: set[str] = set()

        # Start (or continue) a rotation of the stat cursor over the frontier.
        if self._rotation_start is None or self._stat_cursor >= len(self._dirs):
            self._begin_rotation()

        # Per-cycle pending sweep window: a stable snapshot of the pending keys
        # plus a persistent rotating start offset, so a large candidate
        # population is visited over successive cycles (never all every cycle).
        self._pending_keys = list(self._pending.keys())
        self._pending_start = (
            self._pending_rr % len(self._pending_keys)
            if self._pending_keys else 0)
        self._pending_checked = 0

        ops = 0
        stalled = 0
        lane_idx = 0
        while ops < self._op_budget and stalled < 3:
            lane = lane_idx % 3
            if lane == 0:
                did = self._op_stat()
            elif lane == 1:
                did = self._op_enum(ingest)
            else:
                did = self._op_pending(ingest)
            if did:
                ops += 1
                stalled = 0
            else:
                stalled += 1
            lane_idx += 1

        # Persist the pending rotation offset so the next cycle resumes past the
        # candidates already visited this cycle (round-robin continuation).
        if self._pending_keys:
            self._pending_rr = self._pending_start + self._pending_checked

        self._age_pending()
        return ingest

    # ── work lanes (each does at most ONE filesystem op per call) ─────────────

    def _op_stat(self) -> bool:
        """Stat one frontier directory (rotating cursor). A directory that is new
        to the frontier or whose own mtime changed since its last enumeration is
        queued for (re-)enumeration (§5.4 bullet 3). Returns True iff it consumed
        a filesystem op."""
        if self._stat_cursor >= len(self._dirs):
            return False
        d = self._dirs[self._stat_cursor]
        self._stat_cursor += 1
        mt = self._dir_mtime(d)                       # 1 fs op
        if mt is not None:
            prev = self._enum_mtime.get(d, _SENTINEL)
            if (prev is _SENTINEL or prev != mt) and not self._is_queued(d):
                self._enum_queue.append((d, mt))
        if self._stat_cursor >= len(self._dirs):
            self._complete_rotation()
        return True

    def _op_enum(self, ingest: set) -> bool:
        """Enumerate one queued directory (scandir). Records the pre-enumeration
        mtime as the dir's baseline (so a create landing during the enumeration
        is re-examined next rotation), skips symlinked descendants (the
        ``follow_symlinks=False`` boundary), adds real subdirectories to the
        frontier, and registers brand-new ``*.jsonl`` files in ``pending`` BEFORE
        their first ingest — surfacing each for that first ingest attempt in the
        SAME cycle it is discovered (the pending sweep then owns the retries).
        Returns True iff it consumed a filesystem op."""
        if not self._enum_queue:
            return False
        d, mt = self._enum_queue.pop(0)
        entries = self._scandir(d)                    # 1 fs op
        self._enum_mtime[d] = mt
        for (name, path, is_dir, is_sym) in entries:
            if is_sym:
                continue                              # never traverse a symlink
            if is_dir:
                if path not in self._dir_set:
                    self._dir_set.add(path)
                    self._dirs.append(path)           # new subdir joins frontier
            elif path.endswith(".jsonl"):
                if path not in self._known and path not in self._pending:
                    self._pending[path] = 0           # register BEFORE first ingest
                    ingest.add(path)                  # first ingest attempt now
        return True

    def _op_pending(self, ingest: set) -> bool:
        """Check one pending candidate (rotating cursor). Vanished → dropped; no
        committed cursor (brand-new / lock-contended first ingest) → surfaced for
        (re-)ingest; grown beyond its committed cursor → surfaced. Returns True
        iff it consumed a filesystem op."""
        keys = self._pending_keys
        n = len(keys)
        while self._pending_checked < n:
            idx = (self._pending_start + self._pending_checked) % n
            self._pending_checked += 1
            p = keys[idx]
            if p not in self._pending:                # reaped/dropped mid-cycle
                continue
            size = self._file_size(p)                 # 1 fs op
            if size is None:
                self._pending.pop(p, None)            # vanished → drop
            else:
                committed = self._committed.get(p)
                if committed is None or size > committed:
                    ingest.add(p)
            return True
        return False

    # ── rotation + expiry bookkeeping (no filesystem ops) ─────────────────────

    def _begin_rotation(self) -> None:
        self._stat_cursor = 0
        self._rotation_start = self._clock()

    def _complete_rotation(self) -> None:
        # Pinned to the rotation's START time (§5.4 bullet 5): an entry created
        # mid-rotation bumps its own directory's mtime and is re-examined next
        # rotation. The cursor stays at len(_dirs); the next cycle's
        # _begin_rotation resets it.
        self._completed_rotation_watermark = self._rotation_start
        self._rotation_count += 1

    def _is_queued(self, d: str) -> bool:
        return any(qd == d for (qd, _mt) in self._enum_queue)

    def _age_pending(self) -> None:
        expired = []
        for p in list(self._pending.keys()):
            self._pending[p] += 1
            if self._pending[p] > self._pending_expiry:
                expired.append(p)
        for p in expired:
            self._pending.pop(p, None)
