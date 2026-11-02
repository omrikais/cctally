"""#901 spec §6.3 "Drained admission", replaced by #939 spec D6's
tree-independent stability admission: drain a clone's backlog with the tested
tree's ordinary, unbudgeted `cache-sync --source all` in rounds and admit it
when two consecutive rounds are census-complete and leave identical
observations, judged only from evidence main and the branch both provide.

usage: catchup.py --tree TREE --out OUT.json [--deadline-s S]
                  [--source SRC_ROOT [--prep RECEIPT]] [--round-limit N]
       catchup.py prep --source SRC_ROOT --tree TREE [--out RECEIPT]
  (admission runs with the clone's environment: CCTALLY_DATA_DIR on the
   clone, the real transcript roots read-only plus any scratch root, TMPDIR
   external - the runner does this; run it under the interposer so its writes
   are evidence)

`prep` runs once per CLOSED source copy (SRC_ROOT/data, after its drain, with
the tree that will measure it) and writes SRC_ROOT/prep-receipt.json: the
source identity (its path, and each database's size, `user_version` and
WAL/journal size), the tree and its `git rev-parse HEAD`, the instant, and the
sorted `session_files.path` values with `size_bytes > 0` whose filesystem
absence is confirmed - the stored strings, unchanged. Only ENOENT confirms
absence: a permission or I/O error is invalid evidence, never "absent". The
copy is read through `immutable=1` connections, so nothing is created beside
it, and only after its WAL and journal are confirmed empty (a nonempty one
means the copy is not closed, and the receipt is invalid). The receipt is
identity evidence, kept in the admission receipt.

Admission (spec D6, "Workload admission"):
1. Capture the source watermarks (every transcript file under the Claude and
   Codex roots, with its size now) and the logical-identity frontier
   (`frontier.py`'s capture: one entry per logical rollout with its selected
   container, generation and byte-derived complete-record end), written to
   OUT's directory as frontier.json. Neither reads a cursor or a store.
2. Rounds, at most `--round-limit` (12): `cmd_cache_sync(source="all")`
   in-process (all four syncs, wrapped to keep their statistics), then every
   detached Codex worker those syncs requested (`_codex-replay-drain`,
   `_codex-prune-drain`, `_codex-inventory-bootstrap`) run in-process to
   completion, as the dashboard's detached worker would (`WorkerRequests`
   replaces `_spawn_detached`; a #939 D5 tree's attempt id reaches the
   worker), then authoritative `run_stats_ingest` cycles until one consumes
   nothing, then one judgement of the frontier (`frontier.judge`, both stores
   read in one transaction, relative to the previous round's receipt).
3. A round is census-complete when the cache-sync exited 0, every sync
   returned statistics with no lock contention, deferred reason, exhausted
   budget or maintenance failure (CENSUS_FIELDS, fields both trees carry), the
   journal is drained and no worker failed or is still pending. Two
   consecutive census-complete rounds with equal leftover counts
   (LEFTOVER_FIELDS: `files_failed`, `barrier_files_deferred`,
   `barrier_keys_deferred`, `container_pending`), an equal unconsumed set and
   unchanged obligation states admit the clone, provided no frontier entry is
   unconsumed. No stable pair within the limit, a stable pair with an
   unconsumed entry, an outstanding migration or pending maintenance, or a
   drain past the deadline refuses it. Certification results are kept per
   round as evidence only: a production-shaped copy's Codex census never
   certifies, and the certifiable requirement and the Claude stable-residual
   exception route are gone.

The receipt keeps the tree and runtime identities, the watermarks, the
preparation, `stability` (every round's statistics, census problems,
leftovers, worker runs, ingest cycles and judgement; the admitted pair, its
two receipts, every deferred entry with its evidence and obligation state;
the frontier's path and counts; every worker request), the pending state,
the raw cursor gaps (evidence only), the migrations the catch-up applied
(setup work, labelled as such and never steady state) and this process's
lifetime peak physical footprint. Exit 0 ADMITTED (prep: PREPARED), 2 INVALID.

Revision 15 (spec §6.3 "Frozen admission" and "Finite-frontier admission",
Q16, `dc13` L1/L3), by WRITE_ATTRIBUTION_INPUTS:
- frozen:FREEZE - the process must run under that freeze's namespace
  (frozen_roots.require_namespace). Before the drain, the qualification of
  this (store copy, freeze) pair (SRC_ROOT/frozen-qualification.json, which
  also verified the committed prefixes) must name this seal and this copy
  (one session freeze is bound to several copies and qualified once per
  copy, Amendment 11), and every
  retained root key, file identity, physical path string and cursor under a
  frozen root must match the manifest (`frozen_identity_problems`). After the
  catch-up: no retained row pruned, no cursor reset, no new incarnation
  (replay), no re-attribution, and work only on a row whose own pre-drain
  cursor had a frozen suffix (Amendment 14 P2: per row, not the furthest
  cursor across tables; `row_suffixes`), a frozen file the copy did not
  retain, or the controlled scratch input (`relocation_problems`); and the
  unchanged-input verification (`frozen_verification_problems`).
- live (the live-roots confirmation only) - before the catch-up a frontier of
  every discovered file (identity and byte prefix, torn tails flagged) is
  captured and written to OUT.frontier.json; the stable pair's second round
  may process only files whose per-file evidence shows bytes beyond that
  frontier or a first discovery after it (`frontier_problems`, with its
  consumption check off: D6's judgement decides consumption). A cursor reset,
  replay, prune or live discontinuity is INVALID.
The frozen checks compare every round's work against the copy's pre-drain
state (relocation) and the stable pair's second round against its first
(verification).
The receipt's `frontier` names the mode and its evidence.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent

#: The three stores a source copy's identity names.
DATABASES = ("cache.db", "conversations.db", "stats.db")
#: Where `prep` writes, beside the copy's data/ directory.
PREP_RECEIPT = "prep-receipt.json"
#: Spec D6 (#939): the statistics fields BOTH trees carry that decide whether
#: a round's census is complete (a field a tree does not emit reads clean, as
#: the product's own `getattr(..., default)` does) ...
CENSUS_FIELDS = ("lock_contended", "deferred_reason", "budget_exhausted",
                 "maintenance_failed")
#: ... and the per-file leftover counts two consecutive rounds must repeat.
LEFTOVER_FIELDS = ("files_failed", "barrier_files_deferred",
                   "barrier_keys_deferred", "container_pending")
#: Rounds without a stable pair before the clone is refused.
ROUND_LIMIT = 12
#: The detached Codex workers a round runs to completion in-process, as the
#: dashboard's detached worker would (command -> the tree's handler).
CODEX_WORKERS = {
    "_codex-replay-drain": "cmd_codex_replay_drain_internal",
    "_codex-prune-drain": "cmd_codex_prune_drain_internal",
    "_codex-inventory-bootstrap": "cmd_codex_inventory_bootstrap_internal",
}
#: How many times one round re-runs workers that its workers requested.
WORKER_PASSES = 8

#: The one filesystem probe that decides absence (a seam for the self-tests).
_stat = os.stat


def load_tree(tree: pathlib.Path):
    sys.path.insert(0, str(tree / "bin"))
    loader = importlib.machinery.SourceFileLoader(
        "cctally", str(tree / "bin" / "cctally"))
    spec = importlib.util.spec_from_loader("cctally", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = module
    loader.exec_module(module)
    return module


def footprint_peak_bytes():
    sys.path.insert(0, str(HERE))
    from maintenance_op import footprint_peak_bytes as peak
    return peak()


def _git_rev(tree: pathlib.Path) -> "str | None":
    try:
        out = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"],
                             capture_output=True, text=True)
    except OSError:
        return None
    rev = out.stdout.strip()
    return rev if out.returncode == 0 and rev else None


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _roots(env_name: str, default: str, sub: str) -> "list[pathlib.Path]":
    raw = os.environ.get(env_name)
    bases = [p for p in raw.split(",") if p] if raw else [default]
    return [pathlib.Path(os.path.expanduser(b)) / sub for b in bases]


def watermarks() -> dict:
    """{provider: {path: size}} for every transcript file the tree reads."""
    out = {}
    for provider, roots in (
            ("claude", _roots("CLAUDE_CONFIG_DIR", "~/.claude", "projects")),
            ("codex", _roots("CODEX_HOME", "~/.codex", "sessions"))):
        files = {}
        for root in roots:
            for dirpath, _dirs, names in os.walk(root):
                for name in names:
                    if name.endswith(".jsonl"):
                        path = os.path.join(dirpath, name)
                        try:
                            files[path] = os.stat(path).st_size
                        except OSError:
                            continue
        out[provider] = files
    return out


#: (store, cursor table) per provider: each file's consumed byte offset.
CURSORS = {
    "claude": (("cache.db", "session_files"),
               ("conversations.db", "conversation_source_files")),
    "codex": (("cache.db", "codex_session_files"),
              ("conversations.db", "codex_conversation_source_files")),
}


def _ro(path: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def cursor_gaps(data_dir: pathlib.Path, marks: dict) -> "list[str]":
    """Captured watermarks a cursor has not reached yet."""
    gaps = []
    for provider, tables in CURSORS.items():
        for store, table in tables:
            conn = _ro(data_dir / store)
            try:
                cursors = dict(conn.execute(
                    f"SELECT path, last_byte_offset FROM {table}"))
            except sqlite3.Error as exc:
                gaps.append(f"{store}.{table}: {exc}")
                continue
            finally:
                conn.close()
            behind = [p for p, size in marks[provider].items()
                      if size and int(cursors.get(p) or 0) < size]
            if behind:
                gaps.append(f"{store}.{table}: {len(behind)} of "
                            f"{len(marks[provider])} files behind their "
                            f"watermark (e.g. {behind[0]})")
    return gaps


def applied_migrations(data_dir: pathlib.Path) -> dict:
    out = {}
    for store in DATABASES:
        path = data_dir / store
        if not path.exists():
            out[store] = None
            continue
        conn = _ro(path)
        try:
            out[store] = sorted(r[0] for r in conn.execute(
                "SELECT name FROM schema_migrations"))
        except sqlite3.Error:
            out[store] = None
        finally:
            conn.close()
    return out


def user_versions(data_dir: pathlib.Path) -> dict:
    """Each store's `user_version` on this clone, None when unreadable."""
    out = {}
    for store in DATABASES:
        path = data_dir / store
        if not path.exists():
            out[store] = None
            continue
        conn = _ro(path)
        try:
            out[store] = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error:
            out[store] = None
        finally:
            conn.close()
    return out


def _wire(stats):
    """Scalars as they are; lists, tuples and sets explicitly, as sorted lists
    (a `PruneResult`'s `residual_paths` is evidence, not noise)."""
    if stats is None:
        return None
    items = (dataclasses.asdict(stats) if dataclasses.is_dataclass(stats)
             else vars(stats)).items()
    out = {}
    for key, value in items:
        if isinstance(value, (int, float, str, bool, type(None))):
            out[key] = value
        elif isinstance(value, (list, tuple, set, frozenset)):
            out[key] = sorted(str(v) for v in value)
    return out


# ── absence and the tracked-path snapshot ─────────────────────────────────

def _absence(paths) -> "tuple[list[str], list[str]]":
    """(confirmed-absent, errors) over stored path strings. Only a missing
    file (ENOENT) confirms absence; every other error is invalid evidence."""
    absent, errors = [], []
    for path in paths:
        try:
            _stat(path)
        except FileNotFoundError:
            absent.append(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
    return sorted(absent), errors


def _digest(rows) -> dict:
    h = hashlib.sha256()
    for path, size in sorted(rows):
        h.update(f"{path}\t{size}\n".encode("utf-8", "surrogatepass"))
    return {"count": len(rows), "sha256": h.hexdigest()}


def tracked_snapshot(data_dir: pathlib.Path) -> dict:
    """The clone's tracked positive-size Claude paths (stored strings), their
    confirmed-absent subset, and whether cache 001 is applied. Never raises:
    an observation failure is recorded, and the tree's own call still runs."""
    try:
        return _tracked_snapshot(data_dir)
    except Exception as exc:  # noqa: BLE001 - recorded, judged INVALID later
        return {"error": f"{type(exc).__name__}: {exc}"}


def _tracked_snapshot(data_dir: pathlib.Path) -> dict:
    conn = _ro(data_dir / "cache.db")
    try:
        rows = conn.execute("SELECT path, size_bytes FROM session_files "
                            "WHERE size_bytes > 0").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM session_files").fetchone()[0]
        try:
            dedup = conn.execute(
                "SELECT 1 FROM schema_migrations "
                "WHERE name = '001_dedup_highest_wins'").fetchone() is not None
        except sqlite3.OperationalError:
            dedup = None
    except sqlite3.Error as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()
    paths = [p for p, _size in rows]
    absent, errors = _absence(paths)
    return {"paths": set(paths), "absent": absent, "errors": errors,
            "dedup": dedup, "digest": {**_digest(rows), "rows": total}}


class Capture:
    """Wrap the four sync calls `cmd_cache_sync` makes, observe the Claude
    walk's discovery and the `--prune-orphans` helper's structured result.
    Every wrapper returns exactly what the tree's function returns."""

    NAMES = ("sync_cache", "sync_codex_cache", "sync_claude_conversations",
             "sync_codex_conversations")

    def __init__(self, cache_module, data_dir: "pathlib.Path | None" = None):
        self.module = cache_module
        self.data_dir = data_dir
        self.calls = []
        self.prunes = []
        self._walk = None   # the open sync_cache observation, if any
        for name in self.NAMES:
            real = getattr(cache_module, name)
            setattr(cache_module, name, self._wrap(name, real))
        real = getattr(cache_module, "_iter_claude_jsonl_files", None)
        if real is not None:
            setattr(cache_module, "_iter_claude_jsonl_files",
                    self._wrap_discovery(real))
        real = getattr(cache_module, "_prune_orphaned_cache_entries", None)
        if real is not None:
            setattr(cache_module, "_prune_orphaned_cache_entries",
                    self._wrap_prune(real))

    def _wrap(self, name, real):
        def wrapped(*args, **kwargs):
            observe = (name == "sync_cache" and self.data_dir is not None
                       and kwargs.get("only_paths") is None)
            walk = ({"discoveries": [], "thread": threading.get_ident()}
                    if observe else None)
            before = tracked_snapshot(self.data_dir) if observe else None
            started = time.perf_counter()
            self._walk = walk
            try:
                result = real(*args, **kwargs)
            finally:
                self._walk = None
            call = {"name": name, "stats": result,
                    "seconds": time.perf_counter() - started}
            if observe:
                call["claude"] = _observation(
                    walk, before, tracked_snapshot(self.data_dir))
            self.calls.append(call)
            return result
        return wrapped

    def _wrap_discovery(self, real):
        def wrapped(*args, **kwargs):
            walk = self._walk
            # Only the sync_cache call's own thread is its discovery.
            if walk is None or walk["thread"] != threading.get_ident():
                return real(*args, **kwargs)
            record = {"paths": [], "complete": False, "errors": []}
            walk["discoveries"].append(record)

            def observed():
                try:
                    for path in real(*args, **kwargs):
                        record["paths"].append(str(path))
                        yield path
                except GeneratorExit:
                    raise
                except BaseException as exc:
                    record["errors"].append(f"{type(exc).__name__}: {exc}")
                    raise
                record["complete"] = True
            return observed()
        return wrapped

    def _wrap_prune(self, real):
        def wrapped(*args, **kwargs):
            result = real(*args, **kwargs)
            self.prunes.append(result)
            return result
        return wrapped

    def last(self, name):
        found = [c["stats"] for c in self.calls if c["name"] == name]
        return found[-1] if found else None

    def last_claude(self):
        found = [c.get("claude") for c in self.calls
                 if c["name"] == "sync_cache"]
        return found[-1] if found else None


def _observation(walk, before, after) -> dict:
    """One sync_cache call: tracked positive-size paths minus the paths its
    own discovery yielded (walk-missing), and the confirmed-absent subset,
    each before and after the call. The walk's discovery is the LAST
    `_iter_claude_jsonl_files` call inside it (earlier ones are backfills)."""
    discoveries = walk["discoveries"]
    last = discoveries[-1] if discoveries else None
    snapshot_errors = [s["error"] for s in (before, after) if "error" in s]
    out = {
        "dedupApplied": before.get("dedup"),
        "trackedBefore": before.get("digest"),
        "trackedAfter": after.get("digest"),
        "snapshotErrors": snapshot_errors,
        "discovery": None if last is None else {
            "invocations": len(discoveries), "complete": last["complete"],
            "errors": [e for d in discoveries for e in d["errors"]],
            "count": len(last["paths"])},
        "absenceErrors": before.get("errors", []) + after.get("errors", []),
    }
    for when, snap in (("Before", before), ("After", after)):
        ok = last is not None and "error" not in snap
        out["walkMissing" + when] = (
            sorted(snap["paths"] - set(last["paths"])) if ok else None)
        out["confirmedAbsent" + when] = snap.get("absent")
    return out


# ── revision 15 (Q16, `dc13` L1/L3): frozen admission and the live frontier ──

#: The retained cursor rows and the identity columns a relocation or a replay
#: would change, per table (stores as in CURSORS).
RETAINED_COLUMNS = {
    ("cache.db", "session_files"): (
        "last_byte_offset", "size_bytes", "account_key"),
    ("cache.db", "codex_session_files"): (
        "last_byte_offset", "size_bytes", "source_root_key", "account_key",
        "device_id", "inode"),
    ("conversations.db", "conversation_source_files"): (
        "last_byte_offset", "size_bytes", "device_id", "inode",
        "source_incarnation_id", "committed_prefix_sha256"),
    ("conversations.db", "codex_conversation_source_files"): (
        "last_byte_offset", "size_bytes", "source_root_key", "device_id",
        "inode"),
}
#: Columns whose change is a replay (a new incarnation of the same path).
_REPLAY_COLUMNS = ("inode", "source_incarnation_id")
#: Columns whose change re-attributes retained history.
_ATTRIBUTION_COLUMNS = ("source_root_key", "account_key")


def retained_state(data_dir: pathlib.Path) -> dict:
    """{rows: {"table\tpath": {table, path, col...}}, roots: {root key:
    canonical root path}, incarnations: {file identity: incarnation}} read
    from the clone, read-only."""
    rows, roots, incarnations = {}, {}, {}
    for (store, table), columns in RETAINED_COLUMNS.items():
        db = data_dir / store
        if not db.exists():
            continue
        conn = _ro(db)
        try:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            cols = [c for c in columns if c in have]
            if "path" not in have:
                continue
            for row in conn.execute(
                    f"SELECT path{''.join(', ' + c for c in cols)} FROM {table}"):
                rows[f"{table}\t{row[0]}"] = {"table": table, "path": row[0],
                                               **dict(zip(cols, row[1:]))}
            if table == "codex_session_files":
                for query, target in (
                        ("SELECT source_root_key, canonical_root_path "
                         "FROM codex_source_roots", roots),
                        ("SELECT file_identity, incarnation "
                         "FROM codex_file_incarnations", incarnations)):
                    try:
                        target.update(dict(conn.execute(query)))
                    except sqlite3.Error:
                        pass
        finally:
            conn.close()
    return {"rows": rows, "roots": roots, "incarnations": incarnations}


def _under_any(path: str, roots) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def _manifest_files(manifest: dict) -> dict:
    """{logical path: (inode, admitted length)}, symlinks resolved."""
    entries = {e["logical"]: e for e in manifest.get("entries") or []}
    out = {}
    for path, e in entries.items():
        if e.get("kind") == "symlink":
            e = entries.get(e.get("resolved")) or e
        if e.get("kind") == "file":
            out[path] = (e.get("ino"), int(e.get("admittedLength") or 0))
    return out


def frozen_identity_problems(manifest: dict, state: dict, *, root_key,
                             qualification, seal, store_root=None) -> "list[str]":
    """Spec §6.3 "Frozen admission", before the drain: the clone's retained
    root keys, file identities, physical path strings and cursors must match
    the freeze it reads, and the (store copy, freeze) pair must be qualified
    (`frozen_roots.py qualify`, which also verified the committed prefixes).
    A session freeze is bound to several copies and qualified once per copy
    (Amendment 11), so with `store_root` (the copy this clone came from, a
    realpath) the receipt must name that copy. `root_key(canonical path)` is
    the tested tree's own derivation. Pure."""
    problems = []
    if not isinstance(qualification, dict):
        problems.append("no qualification receipt for this store copy and freeze "
                        "(frozen_roots.py qualify)")
    elif qualification.get("qualified") is not True:
        problems.append("the store copy and freeze are not qualified: "
                        + "; ".join((qualification.get("problems") or [])[:3]))
    elif (qualification.get("freeze") or {}).get("sealSha256") != \
            (seal or {}).get("sealSha256"):
        problems.append("the qualification names another freeze "
                        f"({(qualification.get('freeze') or {}).get('sealSha256')})")
    if isinstance(qualification, dict) and store_root is not None:
        named = (qualification.get("store") or {}).get("root")
        if named != store_root:
            problems.append(f"the qualification names another store copy "
                            f"({named}), not {store_root} (frozen_roots.py "
                            f"qualify --store {store_root})")
    logical = [r["logical"] for r in manifest.get("roots") or []]
    files = _manifest_files(manifest)
    absent = {a["logical"] for a in manifest.get("absences") or []}
    for key, canonical in sorted(state.get("roots", {}).items()):
        if _under_any(str(canonical), logical) and root_key(str(canonical)) != key:
            problems.append(f"root key {key} does not derive from its canonical "
                            f"path {canonical} (a relocated root)")
    known_keys = set(state.get("roots", {}))
    for name, row in sorted(state.get("rows", {}).items()):
        path = row["path"]
        if not _under_any(path, logical):
            continue
        if path not in files:
            if path not in absent:
                problems.append(f"{row['table']}: {path} is retained but not in "
                                "the freeze")
            continue
        ino, admitted = files[path]
        if row.get("inode") is not None and ino is not None and int(row["inode"]) != int(ino):
            problems.append(f"{row['table']}: {path} file identity mismatch "
                            f"(stored inode {row['inode']}, frozen {ino})")
        for col in ("last_byte_offset", "size_bytes"):
            if int(row.get(col) or 0) > admitted:
                problems.append(f"{row['table']}: {path} {col} {row.get(col)} is "
                                f"beyond the frozen file ({admitted} bytes)")
        rk = row.get("source_root_key")
        if rk is not None and known_keys and rk not in known_keys:
            problems.append(f"{row['table']}: {path} carries root key {rk}, "
                            "which no retained root derives")
        elif rk is not None and rk in known_keys and not _under_any(
                path, [str(state["roots"][rk])]):
            problems.append(f"{row['table']}: {path} carries root key {rk}, "
                            f"whose canonical root {state['roots'][rk]} does not "
                            "cover it (a relocated root)")
    return problems


def _frozen_lengths(manifest: dict) -> "dict[str, int]":
    """{walked frozen JSONL path: admitted length}. The target of a covered
    link outside every root (a `link-target` root, Amendment 10) is never
    walked under its own spelling: the tree keeps the link's path as its
    cursor row, so its bytes count once, under the link."""
    link_targets = {r["logical"] for r in manifest.get("roots") or []
                    if r.get("origin") == "link-target"}
    return {path: admitted
            for path, (_ino, admitted) in _manifest_files(manifest).items()
            if path.endswith(".jsonl") and path not in link_targets}


def expected_consumption(manifest: dict, state: dict) -> "dict[str, int]":
    """{frozen path: frozen bytes no table of the copy holds yet}: the frozen
    suffix beyond the store copy's FURTHEST cursor across tables for a
    retained file, the whole admitted length for a frozen file the copy does
    not retain. Byte accounting only (the receipt's `expectedFiles` and
    `expectedSuffixBytes`); whether a row's work is explained is decided per
    row against that row's own cursor (`row_suffixes`, Amendment 14 P2)."""
    lengths = _frozen_lengths(manifest)
    furthest = {}
    for row in state.get("rows", {}).values():
        if row["path"] in lengths:
            furthest[row["path"]] = max(furthest.get(row["path"], 0),
                                        int(row.get("last_byte_offset") or 0))
    return {path: max(0, admitted - furthest.get(path, 0))
            for path, admitted in lengths.items()}


def row_suffixes(manifest: dict, state: dict) -> "dict[str, int]":
    """{"table\\tpath": bytes}: for each retained row of a walked frozen file,
    the frozen suffix beyond THAT row's own pre-drain cursor (admitted length
    minus its `last_byte_offset`, never negative). Spec §6.3 "Frozen
    admission" allows work on a retained file only where its cursor has a
    frozen suffix, and a row's own cursor is what decides that: one rollout
    retained by the cache behind its conversations row may catch up although
    the furthest cursor across tables has nothing left (run-s15b-disc-C). The
    keys are exactly the retained rows of frozen files. Pure."""
    lengths = _frozen_lengths(manifest)
    return {name: max(0, lengths[row["path"]] - int(row.get("last_byte_offset") or 0))
            for name, row in state.get("rows", {}).items()
            if row["path"] in lengths}


def _row_changes(before: dict, after: dict) -> "list[str]":
    return [c for c in set(before) | set(after)
            if c not in ("table", "path") and before.get(c) != after.get(c)]


def relocation_problems(before: dict, after: dict, suffixes: dict) -> "list[str]":
    """After the drain of a frozen run: no retained row pruned, no cursor
    reset, no new incarnation (replay), no re-attribution, and work only on
    a row whose own pre-drain cursor had a frozen suffix (`suffixes`, from
    `row_suffixes` over `before`; Amendment 14 P2), new frozen files or paths
    outside the freeze (the controlled scratch input). Pure."""
    problems = []
    for name, row in sorted(before.get("rows", {}).items()):
        path = row["path"]
        frozen = name in suffixes
        new = after.get("rows", {}).get(name)
        if new is None:
            if frozen:
                problems.append(f"{row['table']}: {path} was pruned by the drain")
            continue
        changed = _row_changes(row, new)
        if not changed or not frozen:
            continue
        if int(new.get("last_byte_offset") or 0) < int(row.get("last_byte_offset") or 0):
            problems.append(f"{row['table']}: {path} cursor reset "
                            f"({row.get('last_byte_offset')} -> {new.get('last_byte_offset')})")
        if any(c in changed for c in _REPLAY_COLUMNS):
            problems.append(f"{row['table']}: {path} replay (a new incarnation: "
                            f"{[c for c in changed if c in _REPLAY_COLUMNS]})")
        if any(c in changed and row.get(c) is not None for c in _ATTRIBUTION_COLUMNS):
            problems.append(f"{row['table']}: {path} historical reattribution "
                            f"({[c for c in changed if c in _ATTRIBUTION_COLUMNS]})")
        work = [c for c in changed if c not in _REPLAY_COLUMNS
                and c not in _ATTRIBUTION_COLUMNS and c != "mtime_ns"]
        if work and suffixes.get(name, 0) == 0:
            problems.append(f"{row['table']}: {path} unexplained work on a file "
                            f"without a frozen suffix beyond this row's cursor "
                            f"{int(row.get('last_byte_offset') or 0)} ({sorted(work)})")
    for key, inc in sorted(before.get("incarnations", {}).items()):
        now = after.get("incarnations", {}).get(key)
        if now is not None and now != inc:
            problems.append(f"codex file {key} replay (incarnation {inc} -> {now})")
    return problems


def frozen_verification_problems(after_catchup: dict,
                                 after_verify: dict) -> "list[str]":
    """Amendment 19 HR-12: in a frozen run the verification pass reads the
    same sealed inputs the catch-up consumed, so it may do no work at all:
    no retained row pruned, first tracked, or changed in any column (a
    cursor, a size, an identity, an incarnation or an attribution), and no
    file incarnation replaced. Live mode judges the same comparison against
    its frontier (`frontier_problems`). Pure."""
    problems = []
    before, after = after_catchup.get("rows", {}), after_verify.get("rows", {})
    for name, row in sorted(before.items()):
        new = after.get(name)
        if new is None:
            problems.append(f"{row['table']}: {row['path']} pruned in "
                            "verification")
            continue
        changed = _row_changes(row, new)
        if changed:
            problems.append(f"{row['table']}: {row['path']} changed in "
                            f"verification ({sorted(changed)})")
    for name, row in sorted(after.items()):
        if name not in before:
            problems.append(f"{row['table']}: {row['path']} first tracked in "
                            "verification")
    for key, inc in sorted(after_catchup.get("incarnations", {}).items()):
        now = after_verify.get("incarnations", {}).get(key)
        if now != inc:
            problems.append(f"codex file {key} incarnation {inc} -> {now} in "
                            "verification")
    return problems


def _tail_is_torn(path: str, size: int) -> bool:
    if size <= 0:
        return False
    try:
        with open(path, "rb") as fh:
            fh.seek(size - 1)
            return fh.read(1) != b"\n"
    except OSError:
        return False


def capture_frontier(marks: dict) -> dict:
    """Spec §6.3 "Finite-frontier admission": every discovered file with its
    identity and byte prefix (its size now, and whether that prefix ends in a
    torn record), captured before the catch-up from the same walk as the
    watermarks."""
    files = {}
    for provider, paths in marks.items():
        for path, size in paths.items():
            try:
                st = os.stat(path)
            except OSError:
                continue
            files[path] = {"provider": provider, "dev": st.st_dev,
                           "ino": st.st_ino, "size": size,
                           "torn": _tail_is_torn(path, size)}
    return {"mode": "live", "capturedAt": _utc_now(), "files": files}


def current_files(paths) -> dict:
    out = {}
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        out[path] = {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size}
    return out


def frontier_problems(frontier: dict, after_catchup: dict, after_verify: dict,
                      now: dict, *, check_consumption: bool = True
                      ) -> "tuple[list[str], dict]":
    """Pure. The catch-up must consume the frontier in every store that tracks
    a file; verification may process a file only where per-file evidence
    (`now`, read after it) shows bytes beyond the frontier or a file first
    discovered after it; a cursor reset, a replay, a prune or a live identity
    change is never admitted. Returns (problems, {provider: justified files}).
    #939 D6: `run` passes `check_consumption=False`, because consumption is
    judged by the logical-identity frontier (a covered Codex deferral is not
    a raw cursor gap's failure)."""
    problems = []
    files = frontier.get("files") or {}
    for path, f in sorted(files.items()):
        cur = now.get(path)
        if cur is None or (cur["dev"], cur["ino"]) != (f["dev"], f["ino"]) \
                or cur["size"] < f["size"]:
            problems.append(f"{path}: an unexplained discontinuity after the "
                            "frontier (replaced, shrunk or gone)")
        if not check_consumption:
            continue
        for name, row in after_catchup.get("rows", {}).items():
            if row["path"] == path and int(row.get("last_byte_offset") or 0) < f["size"]:
                problems.append(
                    f"{row['table']}: {path} frontier prefix not consumed "
                    f"({row.get('last_byte_offset')} of {f['size']} bytes)"
                    + (" - torn input at the frontier" if f.get("torn") else ""))
    justified = {"claude": 0, "codex": 0}
    counted = set()
    for name, row in sorted(after_verify.get("rows", {}).items()):
        path = row["path"]
        old = after_catchup.get("rows", {}).get(name)
        f = files.get(path)
        provider = (f or {}).get("provider") or (
            "codex" if "codex" in row["table"] else "claude")
        if old is None:
            if f is not None:
                problems.append(f"{row['table']}: {path} is a frontier file first "
                                "tracked during verification")
            elif (provider, path) not in counted:
                counted.add((provider, path))
                justified[provider] += 1
            continue
        changed = _row_changes(old, row)
        if not changed:
            continue
        if int(row.get("last_byte_offset") or 0) < int(old.get("last_byte_offset") or 0):
            problems.append(f"{row['table']}: {path} cursor reset in verification")
            continue
        if any(c in changed for c in _REPLAY_COLUMNS):
            problems.append(f"{row['table']}: {path} replay in verification "
                            f"({[c for c in changed if c in _REPLAY_COLUMNS]})")
            continue
        grew = f is None or (now.get(path) or {}).get("size", 0) > f["size"]
        if not grew:
            problems.append(f"{row['table']}: {path} unexplained work in "
                            "verification (no bytes beyond the frontier)")
        elif (provider, path) not in counted:
            counted.add((provider, path))
            justified[provider] += 1
    for name, row in sorted(after_catchup.get("rows", {}).items()):
        if name not in after_verify.get("rows", {}):
            problems.append(f"{row['table']}: {row['path']} pruned in verification")
    return problems, justified


# ── #939 D6: tree-independent stability admission (pure) ──────────────────

def _d6():
    """The harness's logical-identity frontier (bench/write-attribution)."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import frontier as d6
    return d6


def round_record(syncs: dict, receipt, *, exit_code, error, ingest=None,
                 workers=None, pending_workers=()) -> dict:
    """One round: its census problems (from CENSUS_FIELDS only, so main and
    the branch are judged alike), its leftover counts (LEFTOVER_FIELDS, None
    where a tree does not report one), its worker runs and its judgement."""
    census = []
    if exit_code != 0:
        census.append(f"census incomplete: the cache-sync exited {exit_code}"
                      + (f" ({error})" if error else ""))
    for name in Capture.NAMES:
        stats = syncs.get(name)
        if stats is None:
            census.append(f"census incomplete: {name} returned no statistics")
            continue
        for field in CENSUS_FIELDS:
            if stats.get(field):
                census.append(f"census incomplete: {name} "
                              f"{field}={stats.get(field)!r}")
    if ingest is not None and (not ingest.get("ran") or ingest.get("consumed")
                               or ingest.get("error")):
        census.append(f"census incomplete: the stats journal is not drained "
                      f"({ingest})")
    for run in workers or []:
        if run.get("error"):
            census.append(f"census incomplete: worker {run['command']} "
                          f"raised {run['error']}")
    if pending_workers:
        census.append("census incomplete: requested workers still pending "
                      f"({sorted(set(pending_workers))})")
    leftovers = {name: {f: (syncs.get(name) or {}).get(f)
                        for f in LEFTOVER_FIELDS} for name in Capture.NAMES}
    return {"exit": exit_code, "error": error, "syncs": syncs,
            "census": census, "leftovers": leftovers, "ingest": ingest,
            "workers": list(workers or []), "receipt": receipt}


def _leftover_changes(a: dict, b: dict) -> "list[str]":
    return [f"{name}.{field} {a.get(name, {}).get(field)} -> "
            f"{b.get(name, {}).get(field)}"
            for name in sorted(set(a) | set(b)) for field in LEFTOVER_FIELDS
            if a.get(name, {}).get(field) != b.get(name, {}).get(field)]


def stability(rounds: list, limit: int = ROUND_LIMIT) -> dict:
    """Spec D6 workload admission over the rounds so far. Admitted at the
    first pair of consecutive rounds that are census-complete and leave
    identical observations (leftover counts, the unconsumed set, and every
    deferred entry's obligation state relative to the pair's first round)
    with no unconsumed entry. `final` says no further round can change the
    verdict: a stable pair with an unconsumed entry, or the round limit."""
    d6 = _d6()
    n = len(rounds)
    out = {"admitted": False, "final": n >= limit, "pair": None,
           "receipts": None, "deferred": [], "problems": []}
    if n < 2:
        out["problems"] = ([f"no stable pair within {limit} rounds"]
                           if out["final"] else ["fewer than two rounds"])
        return out
    a, b = rounds[-2], rounds[-1]
    ia, ib = n - 2, n - 1
    ra, rb = a.get("receipt"), b.get("receipt")
    problems = ([f"round {ia}: {p}" for p in a["census"]]
                + [f"round {ib}: {p}" for p in b["census"]])
    changed = _leftover_changes(a["leftovers"], b["leftovers"])
    if changed:
        problems.append(f"leftover counts changed between rounds {ia} and "
                        f"{ib}: " + "; ".join(changed[:8]))
    partial = (d6.receipt_problems(ra, f"round {ia}")
               + d6.receipt_problems(rb, f"round {ib}"))
    problems += partial
    unconsumed = []
    if not partial:
        ua = sorted(e["logical"] for e in ra["unconsumed"])
        ub = sorted(e["logical"] for e in rb["unconsumed"])
        if ua != ub:
            problems.append(f"the unconsumed entries changed between rounds "
                            f"{ia} and {ib} ({len(ua)} -> {len(ub)})")
        problems += d6.obligation_changes(ra, ra, rb)
        unconsumed = d6.unconsumed_problems(rb, f"round {ib}")
    if not problems and not unconsumed:
        ref = {e["logical"]: e for e in ra["deferred"]}
        out.update({
            "admitted": True, "final": True, "pair": [ia, ib],
            "receipts": [ra, rb],
            "deferred": [dict(e, obligationState=d6.obligation_state(
                e, ref.get(e["logical"]))) for e in rb["deferred"]]})
        return out
    if not problems:
        out.update({"final": True, "pair": [ia, ib], "receipts": [ra, rb],
                    "problems": unconsumed})
        return out
    if out["final"]:
        problems.append(f"no stable pair within {limit} rounds")
    out["problems"] = problems
    return out


class WorkerRequests:
    """Spec D6: the detached workers a round's syncs request, run in-process
    to completion as the dashboard's detached worker would. The tree's
    `_spawn_detached` is replaced for the run: a Codex worker command is
    recorded with the environment its spawner scoped (a #939 D5 tree's
    attempt id, so the worker records and retires its own attempt) and is
    reported spawned; any other command is recorded and reported not spawned
    (in this process `sys.argv[0]` is this script, never the tree's CLI)."""

    def __init__(self, update_module):
        self.pending = []
        self.log = []
        current = getattr(update_module, "detached_env_current", None)

        def spawn(command, *, env=None):
            scoped = dict(current() if current is not None else {})
            scoped.update(env or {})
            worker = command in CODEX_WORKERS
            self.log.append({"command": command, "worker": worker})
            if worker:
                self.pending.append((command, scoped))
            return worker

        update_module._spawn_detached = spawn

    def run(self, c) -> "list[dict]":
        """Run every requested worker, then those they requested, up to
        WORKER_PASSES times; what is left stays in `pending`."""
        runs = []
        for _ in range(WORKER_PASSES):
            if not self.pending:
                break
            batch, self.pending = self.pending, []
            for command, env in batch:
                handler = getattr(c, CODEX_WORKERS[command], None)
                saved = {k: os.environ.get(k) for k in env}
                os.environ.update({k: str(v) for k, v in env.items()})
                t0 = time.monotonic()
                try:
                    if handler is None:
                        code, error = None, "the tree has no handler"
                    else:
                        code, error = handler(argparse.Namespace()), None
                except Exception as exc:  # noqa: BLE001 - kept as evidence
                    code, error = None, f"{type(exc).__name__}: {exc}"
                finally:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                runs.append({"command": command, "exit": code, "error": error,
                             "seconds": round(time.monotonic() - t0, 3)})
        return runs


# ── explicit migration and pending-maintenance state ──────────────────────

def pending_state(data_dir: pathlib.Path, *, registries: dict,
                  stats_epoch, flags, aliases=None) -> dict:
    """Migration state and pending maintenance, read from the clone. Cursor
    gaps (`pendingWork`) cannot show either: an empty list proves neither the
    absence of an outstanding migration nor of pending backfill work."""
    aliases = aliases or {}
    flags = tuple(flags or ())
    state = {"migrations": {}, "maintenanceFlags": {}, "stats": {},
             "errors": [] if flags else [
                 "the tested tree names no pending-maintenance keys"]}
    for db in ("cache.db", "conversations.db"):
        names = list(registries.get(db) or [])
        entry = {"userVersion": None, "registry": len(names),
                 "outstanding": None, "skipped": None, "error": None}
        found = None
        path = pathlib.Path(data_dir) / db
        if not names:
            entry["error"] = "the tested tree exposes no migration registry"
        elif not path.exists():
            entry["error"] = "missing"
        else:
            conn = _ro(path)
            try:
                entry["userVersion"] = conn.execute(
                    "PRAGMA user_version").fetchone()[0]
                alias = aliases.get(db) or {}
                applied = {alias.get(n, n) for (n,) in conn.execute(
                    "SELECT name FROM schema_migrations")}
                try:
                    skipped = {n for (n,) in conn.execute(
                        "SELECT name FROM schema_migrations_skipped")}
                except sqlite3.OperationalError:
                    skipped = set()
                entry["outstanding"] = [n for n in names if n not in applied
                                        and n not in skipped]
                entry["skipped"] = sorted(n for n in names if n in skipped)
                if flags:
                    marks = ",".join("?" for _ in flags)
                    found = sorted(k for (k,) in conn.execute(
                        f"SELECT key FROM cache_meta WHERE key IN ({marks})",
                        flags))
            except sqlite3.Error as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                conn.close()
        state["migrations"][db] = entry
        state["maintenanceFlags"][db] = found
    stats = {"userVersion": None, "epoch": stats_epoch,
             "projectionIncomplete": None, "error": None}
    path = pathlib.Path(data_dir) / "stats.db"
    if not path.exists():
        stats["error"] = "missing"
    else:
        conn = _ro(path)
        try:
            stats["userVersion"] = conn.execute(
                "PRAGMA user_version").fetchone()[0]
            try:
                row = conn.execute("SELECT incomplete FROM "
                                   "stats_quota_projection_state WHERE id = 1"
                                   ).fetchone()
                stats["projectionIncomplete"] = (
                    None if row is None else bool(int(row[0] or 0)))
            except sqlite3.OperationalError:
                pass   # no projection state: nothing was ever marked
        except sqlite3.Error as exc:
            stats["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()
    state["stats"] = stats
    return state


def pending_problems(state: dict) -> "list[str]":
    problems = []
    for db, entry in state["migrations"].items():
        if entry["error"]:
            problems.append(f"{db}: migration state unreadable "
                            f"({entry['error']})")
        elif entry["outstanding"]:
            problems.append(f"{db}: {len(entry['outstanding'])} migration(s) "
                            f"outstanding: {', '.join(entry['outstanding'])}")
    for db, keys in state["maintenanceFlags"].items():
        if keys:
            problems.append(f"{db}: pending maintenance outstanding "
                            f"({', '.join(keys)})")
    stats = state["stats"]
    if stats["error"]:
        problems.append(f"stats.db: state unreadable ({stats['error']})")
    elif stats["epoch"] is None:
        problems.append("stats.db: the tested tree names no stats epoch")
    elif stats["userVersion"] != stats["epoch"]:
        problems.append(f"stats.db is at version {stats['userVersion']}, not "
                        f"the tested tree's epoch {stats['epoch']}")
    if stats.get("projectionIncomplete"):
        problems.append("stats.db: the quota projection is marked incomplete "
                        "(recovery outstanding)")
    problems.extend(state.get("errors") or [])
    return problems


def tree_pending_inputs(c, cache, frontier) -> dict:
    """The tested tree's own registries, stats epoch and pending keys."""
    db = c._load_sibling("_cctally_db")
    core = c._load_sibling("_cctally_core")
    flags = set()
    for module, name in ((frontier, "_PENDING_META_KEYS"),
                         (frontier, "_CONVERSATION_PENDING_META_KEYS"),
                         (cache, "_TARGETED_DECLINE_FLAGS")):
        flags.update(getattr(module, name, ()) or ())
    return {"registries": {
                "cache.db": [m.name for m in db._CACHE_MIGRATIONS],
                "conversations.db": [m.name
                                     for m in db._CONVERSATIONS_MIGRATIONS]},
            "stats_epoch": getattr(core, "STATS_INDEX_EPOCH", None),
            "flags": tuple(sorted(flags)),
            "aliases": getattr(db, "_LEGACY_MARKER_ALIASES_BY_DB", {})}


# ── the preparation receipt ───────────────────────────────────────────────

def _immutable(path: pathlib.Path) -> sqlite3.Connection:
    # Safe ONLY after the caller confirmed the WAL and journal are empty:
    # `immutable=1` skips them, and creates nothing beside the file.
    return sqlite3.connect(pathlib.Path(path).as_uri() + "?mode=ro&immutable=1",
                           uri=True)


def prepare(source, tree) -> "tuple[int, dict]":
    """The preparation receipt of a CLOSED source copy (module docstring)."""
    root = pathlib.Path(os.path.realpath(source))
    data = root / "data"
    tree = pathlib.Path(tree).resolve()
    problems = []
    receipt = {"kind": "catchup-prep", "schemaVersion": 1,
               "createdAt": _utc_now(),
               "source": {"root": str(root), "dataDir": str(data),
                          "databases": {}},
               "tree": {"path": str(tree), "gitRev": _git_rev(tree)},
               "trackedPositive": None, "residual": [], "absenceErrors": [],
               "valid": False, "problems": problems}
    if receipt["tree"]["gitRev"] is None:
        problems.append(f"the tree's git revision is unknown ({tree})")
    seen, unreadable = {}, []
    for db in DATABASES:
        path = data / db
        try:
            st = os.stat(path)
        except OSError as exc:
            unreadable.append(f"{db}: {type(exc).__name__}: {exc}")
            continue
        entry = {"bytes": st.st_size, "mtimeNs": st.st_mtime_ns,
                 "userVersion": None}
        for suffix, key in (("-wal", "walBytes"), ("-journal", "journalBytes")):
            try:
                entry[key] = os.stat(f"{path}{suffix}").st_size
            except FileNotFoundError:
                entry[key] = 0
            except OSError as exc:
                entry[key] = None
                unreadable.append(f"{db}{suffix}: {type(exc).__name__}: {exc}")
            if entry[key]:
                unreadable.append(
                    f"{db}{suffix} holds {entry[key]} bytes: the source copy "
                    "is not closed (checkpoint it, then prepare again)")
        receipt["source"]["databases"][db] = entry
        seen[db] = (st.st_size, st.st_mtime_ns)
    problems.extend(unreadable)
    # Read only a provably closed copy: `immutable=1` would skip a WAL.
    if not unreadable:
        rows = None
        for db in DATABASES:
            try:
                conn = _immutable(data / db)
                try:
                    receipt["source"]["databases"][db]["userVersion"] = \
                        conn.execute("PRAGMA user_version").fetchone()[0]
                    if db == "cache.db":
                        rows = conn.execute(
                            "SELECT path, size_bytes FROM session_files "
                            "WHERE size_bytes > 0").fetchall()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                problems.append(f"{db}: {type(exc).__name__}: {exc}")
        if rows is not None:
            text = [p for p, _s in rows if isinstance(p, str)]
            if len(text) != len(rows):
                problems.append("session_files holds a non-text path")
            receipt["trackedPositive"] = _digest(rows)
            absent, errors = _absence(text)
            receipt["residual"] = absent
            receipt["absenceErrors"] = errors
            problems.extend(f"absence unconfirmed: {e}" for e in errors)
        for db, (size, mtime) in seen.items():
            try:
                st = os.stat(data / db)
                now = (st.st_size, st.st_mtime_ns)
            except OSError:
                now = None
            if now != (size, mtime):
                problems.append(f"{db} changed while it was read: the source "
                                "copy is not closed")
    receipt["valid"] = not problems
    return (0 if not problems else 2), receipt


def _write_json(path: pathlib.Path, payload: dict) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True,
                              default=str) + "\n")
    os.replace(tmp, path)


def _read_prep(path) -> "tuple[dict | None, str | None]":
    if path is None:
        return None, None
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict) or data.get("kind") != "catchup-prep":
        return None, "not a catchup.py prep receipt"
    return data, None


# ── admission ─────────────────────────────────────────────────────────────

def _input_mode() -> "tuple[str | None, str | None]":
    """(mode, freeze): ("frozen", FREEZE), ("live", None), or (None, None)
    when no input mode is set (the pre-revision-15 behaviour, kept for the
    hermetic tests; every runner sets one)."""
    raw = os.environ.get("WRITE_ATTRIBUTION_INPUTS", "")
    if raw.startswith("frozen:"):
        return "frozen", raw[len("frozen:"):]
    return ("live", None) if raw == "live" else (None, None)


def _ingest_cycles(journal, started, deadline_s) -> "list[dict]":
    """Authoritative stats-journal ingest until one cycle consumes nothing."""
    cycles = []
    while time.monotonic() - started < deadline_s:
        result = journal.run_stats_ingest(mode="authoritative", timeout_s=60.0)
        cycles.append({"ran": result.ran, "consumed": result.consumed,
                       "malformed": result.malformed,
                       "error": None if result.error is None
                       else repr(result.error)})
        if not result.ran or not result.consumed or result.error:
            break
    return cycles


def _frontier_path(out) -> "pathlib.Path | None":
    """Spec D6's frontier capture, written beside the receipt (a workload run
    directory's frontier.json)."""
    return None if out is None else pathlib.Path(out).parent / "frontier.json"


def run(tree: pathlib.Path, deadline_s: float, *, source=None,
        prep_path=None, out=None, limit: int = ROUND_LIMIT
        ) -> "tuple[int, dict]":
    import argparse as _argparse

    started = time.monotonic()
    data_dir = pathlib.Path(os.environ["CCTALLY_DATA_DIR"])
    rev = _git_rev(tree)
    source_root = None if source is None else os.path.realpath(source)
    if prep_path is None and source_root is not None:
        prep_path = pathlib.Path(source_root) / PREP_RECEIPT
    prep, prep_error = _read_prep(prep_path)
    identity = {"gitRev": rev, "sourceRoot": source_root,
                "userVersions": user_versions(data_dir)}
    before_migrations = applied_migrations(data_dir)
    mode, freeze = _input_mode()
    marks = watermarks()
    live_frontier = capture_frontier(marks) if mode == "live" else None
    c = load_tree(tree)
    cache = c._load_sibling("_cctally_cache")
    journal = c._load_sibling("_cctally_journal")
    frontier = c._load_sibling("_lib_ingest_frontier")
    update = c._load_sibling("_cctally_update")
    # #939 D6: the logical-identity frontier, captured before any round from
    # the inputs this process reads (never from a cursor or a store).
    d6 = _d6()
    libs = d6.load_libs(tree)
    d6_frontier = d6.capture(d6.env_roots(os.environ), libs)
    d6_path = _frontier_path(out)
    if d6_path is not None:
        _write_json(d6_path, d6_frontier)
    # Revision 15 (Q16): the frozen identity checks run BEFORE the drain.
    frozen = None
    if mode == "frozen":
        sys.path.insert(0, str(HERE))
        import frozen_roots
        manifest = json.loads((pathlib.Path(freeze) / "manifest.json").read_text())
        identity_lib = c._load_sibling("_lib_source_identity")
        qualification = None
        if source_root is not None:
            qpath = pathlib.Path(source_root) / "frozen-qualification.json"
            if qpath.exists():
                qualification = json.loads(qpath.read_text())
        before_state = retained_state(data_dir)
        seal = frozen_roots.seal_identity(freeze)
        frozen = {"manifest": manifest, "seal": seal, "before": before_state,
                  "identityProblems": frozen_identity_problems(
                      manifest, before_state,
                      root_key=identity_lib.source_root_key,
                      qualification=qualification, seal=seal,
                      store_root=source_root),
                  "expected": expected_consumption(manifest, before_state),
                  "suffixes": row_suffixes(manifest, before_state),
                  "qualification": qualification}
    capture = Capture(cache, data_dir)
    workers = WorkerRequests(update)
    receipt = {"tree": str(tree), "gitRev": rev,
               "python": sys.version.split()[0],
               "sqlite": sqlite3.sqlite_version,
               "watermarks": {p: {"files": len(f), "bytes": sum(f.values())}
                              for p, f in marks.items()},
               "deadlineS": deadline_s, "identity": identity,
               "preparation": {"source": source_root,
                               "path": None if prep_path is None
                               else str(prep_path),
                               "present": prep is not None,
                               "error": prep_error, "receipt": prep}}
    args = _argparse.Namespace(source="all", rebuild=False,
                               prune_orphans=False, prune_conversations=False)
    rounds, states, verdict = [], [], None
    while True:
        t0 = time.monotonic()
        capture.calls.clear()
        try:
            code, error = c.cmd_cache_sync(args), None
        except Exception as exc:  # noqa: BLE001 - kept as evidence
            code, error = None, f"{type(exc).__name__}: {exc}"
        syncs = {name: _wire(capture.last(name)) for name in Capture.NAMES}
        # The original certification results, kept as evidence only: a
        # production-shaped copy's Codex census never certifies (D6).
        certification = {
            name: bool(frontier.provider_sync_certifiable(
                "full", capture.last(name)))
            for name in ("sync_cache", "sync_codex_cache")}
        certification.update({
            name: bool(frontier.conversation_sync_certifiable(
                "full", capture.last(name)))
            for name in ("sync_claude_conversations",
                         "sync_codex_conversations")})
        claude = capture.last_claude()
        worker_runs = workers.run(c)
        cycles = _ingest_cycles(journal, started, deadline_s)
        reference = rounds[-1]["receipt"] if rounds else None
        try:
            judgement = d6.judge_dir(d6_frontier, data_dir, libs,
                                     reference=reference)
        except Exception as exc:  # noqa: BLE001 - a missing judgement refuses
            judgement = {"complete": False,
                         "error": f"{type(exc).__name__}: {exc}"}
        record = round_record(
            syncs, judgement, exit_code=code, error=error,
            ingest=cycles[-1] if cycles else {"ran": False},
            workers=worker_runs,
            pending_workers=[cmd for cmd, _ in workers.pending])
        record.update({"seconds": round(time.monotonic() - t0, 3),
                       "certification": certification, "claude": claude,
                       "ingestCycles": cycles})
        rounds.append(record)
        states.append(retained_state(data_dir))
        verdict = stability(rounds, limit)
        if verdict["final"] or time.monotonic() - started >= deadline_s:
            break
    pair = verdict["pair"] or [max(0, len(rounds) - 2), len(rounds) - 1]
    after_catchup, after_verify = states[pair[0]], states[pair[1]]
    frontier_receipt, mode_problems = None, []
    if mode == "frozen":
        # Every round's work against the copy's pre-drain retained state, and
        # the stable pair's second round against its first (HR-12).
        relocation = relocation_problems(frozen["before"], states[-1],
                                         frozen["suffixes"])
        verification = frozen_verification_problems(after_catchup, after_verify)
        mode_problems = frozen["identityProblems"] + relocation + verification
        frontier_receipt = {
            "mode": "frozen", "freeze": frozen["seal"],
            "qualified": (frozen["qualification"] or {}).get("qualified"),
            "expectedFiles": sum(1 for v in frozen["expected"].values() if v),
            "expectedSuffixBytes": sum(frozen["expected"].values()),
            # Amendment 14 P2: the retained rows whose own cursor had a
            # frozen suffix, and those suffixes' bytes summed per row (a
            # rollout behind in one table and caught up in another counts
            # only in the table that is behind)
            "expectedRows": sum(1 for v in frozen["suffixes"].values() if v),
            "expectedRowSuffixBytes": sum(frozen["suffixes"].values()),
            "identityProblems": frozen["identityProblems"],
            "relocationProblems": relocation,
            # HR-12: the stable pair's second round against its first
            "verification": {"checked": True, "problems": verification}}
    elif mode == "live":
        now = current_files(sorted(set(live_frontier["files"]) | {
            row["path"] for row in after_verify["rows"].values()}))
        mode_problems, justified = frontier_problems(
            live_frontier, after_catchup, after_verify, now,
            check_consumption=False)
        frontier_path = None if out is None else pathlib.Path(
            str(out) + ".frontier.json")
        if frontier_path is not None:
            _write_json(frontier_path, live_frontier)
        files = live_frontier["files"]
        frontier_receipt = {
            "mode": "live", "capturedAt": live_frontier["capturedAt"],
            "path": None if frontier_path is None else str(frontier_path),
            "files": len(files),
            "bytes": sum(f["size"] for f in files.values()),
            "torn": sum(1 for f in files.values() if f.get("torn")),
            "justifiedInVerification": justified,
            "grewAfterFrontier": sum(
                1 for p, f in files.items()
                if (now.get(p) or {}).get("size", 0) > f["size"]),
            "problems": mode_problems}
    try:
        pending = pending_state(data_dir,
                                **tree_pending_inputs(c, cache, frontier))
    except Exception as exc:  # noqa: BLE001 - missing evidence is INVALID
        pending = {"migrations": {}, "maintenanceFlags": {},
                   "stats": {"error": None, "epoch": 0, "userVersion": 0},
                   "errors": [f"pending state unreadable: "
                              f"{type(exc).__name__}: {exc}"]}
    pending_issues = pending_problems(pending)
    problems = list(d6.frontier_problems(d6_frontier))
    if not verdict["admitted"]:
        problems.extend(verdict["problems"] or ["no stable pair of rounds"])
    problems.extend(pending_issues)
    problems.extend(mode_problems)
    elapsed = time.monotonic() - started
    if elapsed > deadline_s:
        problems.append(f"the drain took {elapsed:.0f} s, past the "
                        f"{deadline_s:.0f} s admission deadline")
    after_migrations = applied_migrations(data_dir)
    receipt.update({
        "inputMode": mode, "frontier": frontier_receipt,
        "stability": {**verdict, "limit": limit, "rounds": rounds,
                      "frontierPath": None if d6_path is None
                      else str(d6_path),
                      "frontierCounts": d6_frontier["counts"],
                      "workerRequests": workers.log},
        "elapsedS": round(elapsed, 3),
        # Raw cursor gaps against the watermarks: evidence only, because a
        # covered Codex deferral leaves one by design (D6 judges consumption).
        "pendingWork": cursor_gaps(data_dir, marks),
        "pendingState": {**pending, "problems": pending_issues},
        # One-time migration and index builds during catch-up are SETUP work
        # (§5.3a): recorded, labelled, never folded into steady state.
        "setupMigrations": {
            store: sorted(set(after_migrations.get(store) or [])
                          - set(before_migrations.get(store) or []))
            for store in after_migrations},
        "footprintPeakBytes": footprint_peak_bytes(),
        "pid": os.getpid(),
        "admitted": not problems, "problems": problems,
    })
    return (0 if not problems else 2), receipt


def prep_main(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="catchup.py prep",
        description="Write the preparation receipt of a CLOSED source copy.")
    parser.add_argument("--source", required=True, type=pathlib.Path,
                        help="SRC_ROOT (its data/ is the closed copy)")
    parser.add_argument("--tree", required=True, type=pathlib.Path,
                        help="the tree that will measure this copy")
    parser.add_argument("--out", type=pathlib.Path,
                        help=f"default: SRC_ROOT/{PREP_RECEIPT}")
    args = parser.parse_args(argv)
    code, receipt = prepare(args.source, args.tree)
    out = args.out or pathlib.Path(receipt["source"]["root"]) / PREP_RECEIPT
    if not out.parent.is_dir():
        print(f"INVALID: no directory for the receipt ({out.parent})")
        return 2
    _write_json(out, receipt)
    print(f"PREPARED: {len(receipt['residual'])} confirmed-absent residual "
          f"path(s) -> {out}" if code == 0 else
          "INVALID: " + "; ".join(receipt["problems"]))
    return code


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sys.path.insert(0, str(HERE))
    import frozen_roots
    frozen_roots.require_namespace("catchup.py")
    if argv[:1] == ["prep"]:
        return prep_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--deadline-s", type=float, default=900.0)
    parser.add_argument("--source", type=pathlib.Path,
                        help="SRC_ROOT the clone was made from (its "
                             "preparation receipt and frozen qualification)")
    parser.add_argument("--prep", type=pathlib.Path,
                        help=f"default: SRC_ROOT/{PREP_RECEIPT}")
    parser.add_argument("--round-limit", type=int, default=ROUND_LIMIT)
    args = parser.parse_args(argv)
    code, receipt = run(args.tree.resolve(), args.deadline_s,
                        source=args.source, prep_path=args.prep, out=args.out,
                        limit=args.round_limit)
    args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True,
                                   default=str) + "\n")
    stab = receipt["stability"]
    print(f"ADMITTED (stable rounds {stab['pair']}, "
          f"{len(stab['deferred'])} deferred entr"
          f"{'y' if len(stab['deferred']) == 1 else 'ies'})"
          if code == 0 else "INVALID: " + "; ".join(receipt["problems"]))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
