"""Pure watch-step kernel for the conversation live-tail (spec §2.4).

No I/O of its own — the filesystem `stat` and the targeted ingest are injected
as callables so the cycle is unit-testable without real timing, threads, or a
DB. The thin SSE/sleep/keep-alive driver lives in bin/_cctally_dashboard.py.
"""


def file_sig(path):
    """(st_size, st_mtime_ns) for a path, or None if it can't be stat'd
    (deleted / rotated). Pure-ish — the only I/O, isolated here so callers can
    inject a fake in tests."""
    import os
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def changed_paths(files, seen, stat_fn=file_sig):
    """Paths whose current signature differs from `seen`. An unstatable path
    (stat_fn → None) is skipped (dropped this cycle, re-resolved later); a path
    absent from `seen` counts as changed (first observation / pre-connect
    growth)."""
    out = []
    for p in files:
        sig = stat_fn(p)
        if sig is None:
            continue
        if seen.get(p) != sig:
            out.append(p)
    return out


def watch_step(files, seen, *, stat_fn=file_sig, ingest_fn, committed_sig_fn=None):
    """One watch cycle. Returns (new_seen, emitted).

    Detect changed files → run ingest_fn(changed) (targeted sync_cache). Emit +
    advance `seen` ONLY on a clean ingest (stats.targeted_clean). `seen` is
    advanced to the COMMITTED cache cursor (committed_sig_fn) — NOT a fresh
    filesystem re-stat — so a file that grew during the ingest is still seen as
    changed next cycle (the cache cursor, in session_files, lags the new disk
    size). committed_sig_fn defaults to stat_fn for pure unit tests with no cache.
    A contended/declined/failed ingest leaves `seen` untouched so the next cycle
    retries (the 5s backstop is the floor)."""
    committed_sig_fn = committed_sig_fn or stat_fn
    changed = changed_paths(files, seen, stat_fn)
    if not changed:
        return seen, False
    stats = ingest_fn(changed)
    if not getattr(stats, "targeted_clean", False):
        return seen, False
    new_seen = dict(seen)
    for p in changed:
        sig = committed_sig_fn(p)
        if sig is not None:
            new_seen[p] = sig
    return new_seen, True
