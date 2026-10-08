"""#901 spec §6.3 "Drained admission" (Q11, 901-PA-002; revision 10, `dc8`):
drain a clone's backlog with the tested tree's ordinary, unbudgeted
`cache-sync --source all` and admit it only when every certifiable-sync
predicate holds - or, for Claude accounting alone, the harness-only
stable-residual exception does.

usage: catchup.py --tree TREE --out OUT.json [--deadline-s S]
                  [--source SRC_ROOT [--prep RECEIPT]]
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
means the copy is not closed, and the receipt is invalid).

Admission steps, all inside the deadline (the runner's 900-second admission
budget, shared with the dashboard's warm admission):
1. Capture the source watermarks: every transcript file under the Claude and
   Codex roots the tree reads, with its size now.
2. Catch up: `cmd_cache_sync(source="all")` in-process, with the four sync
   calls wrapped to keep their returned statistics, then the stats journal
   drained by authoritative `run_stats_ingest` cycles until one consumes
   nothing. Around each `sync_cache` call the harness also records the tracked
   positive-size `session_files` paths before and after, the paths the tree's
   `_iter_claude_jsonl_files` actually yields (observed, never changed), and
   whether cache migration `001_dedup_highest_wins` was applied first.
3. Verify: a second `cache-sync` pass and one more ingest cycle.
4. Admit only when the Codex accounting provider passes
   `provider_sync_certifiable("full", stats)`, both conversation providers
   `conversation_sync_certifiable("full", stats)`, and Claude accounting its
   ordinary full-walk predicate or the stable-residual exception
   (`claude_residual_admission`, evaluated only when `--source` names the
   copy; its prune evidence comes from the tree's own `--prune-orphans` path
   on this clone). Both routes also need both cache-syncs to exit 0, every
   captured watermark consumed, the last ingest cycle consuming nothing
   without error, no outstanding migration, pending maintenance or incomplete
   quota projection, and the whole drain inside the deadline. Zero backlog
   counters alone are never evidence: an unbudgeted Codex call leaves them at
   zero by construction. The exception never mints or alters a product
   certificate; the original certification results are kept beside it.

The receipt keeps the tree and runtime identities, the watermarks, the
elapsed time, every returned statistic, the per-pass Claude observations, the
preparation identity and residual, the structured prune result, the original
certification results, each exception check and its verdict, the pending
state, the migrations the catch-up applied (setup work, labelled as such and
never steady state) and this process's lifetime peak physical footprint.
Exit 0 ADMITTED (prep: PREPARED), 2 INVALID.

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
  retain, or the controlled scratch input (`relocation_problems`). The unchanged-input verification
  and the revision-10 residual exception follow unchanged.
- live (the live-roots confirmation only) - before the catch-up a frontier of
  every discovered file (identity and byte prefix, torn tails flagged) is
  captured and written to OUT.frontier.json; every store must consume it;
  verification may process only files whose per-file evidence shows bytes
  beyond the frontier or a first discovery after it (`frontier_problems`),
  and the Claude residual route admits exactly that many processed files.
  A cursor reset, replay, prune, live discontinuity or torn frontier tail is
  INVALID; the 900-second deadline is unchanged.
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
PASSES = ("catchUp", "verify")
#: `provider_sync_certifiable`'s common_clean block, restated over serialized
#: statistics (a test pins the two together). A field the statistics do not
#: carry reads clean, exactly as the product's `getattr(..., default)` does.
COMMON_CLEAN_FIELDS = ("lock_contended", "files_failed", "files_deferred_torn",
                       "deferred_reason", "prune_refused", "budget_exhausted",
                       "maintenance_failed")
RESIDUAL_SETS = ("walkMissingBefore", "walkMissingAfter",
                 "confirmedAbsentBefore", "confirmedAbsentAfter")
EXCEPTION_ROUTE = "stable-residual exception"

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


# ── the stable-residual exception (pure) ──────────────────────────────────

def residual_exception_checks(prep, passes, prune, *, identity=None,
                              justified_claude=None) -> "list[dict]":
    """Every check of spec §6.3 revision 10's harness-only exception, in
    order, each {check, ok, detail}. Pure: it reads only its arguments, which
    are the serialized forms `catchup.json` keeps, so the verdict can be
    recomputed from the receipt alone."""
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok),
                       "detail": "" if ok else detail})

    residual = None
    if not isinstance(prep, dict):
        check("preparation.present", False,
              "no preparation receipt for this source copy (run `catchup.py "
              "prep` on it after its drain, with the tree that measures it)")
    else:
        check("preparation.present", True, "")
        valid = prep.get("valid") is True and not prep.get("problems")
        check("preparation.valid", valid,
              "the preparation receipt is invalid: "
              + ("; ".join(prep.get("problems") or []) or "not marked valid"))
        listed = prep.get("residual")
        if valid and isinstance(listed, list):
            residual = sorted(listed)
        check("preparation.residual", bool(residual),
              "the preparation recorded no orphan residual; the exception "
              "admits only a nonempty one")
        ident = identity if isinstance(identity, dict) else {}
        rev = (prep.get("tree") or {}).get("gitRev")
        check("preparation.tree", bool(rev) and rev == ident.get("gitRev"),
              f"prepared with tree {rev}, measured with {ident.get('gitRev')}")
        root = (prep.get("source") or {}).get("root")
        check("preparation.source",
              bool(root) and root == ident.get("sourceRoot"),
              f"prepared for source {root}, run from "
              f"{ident.get('sourceRoot')}")
        dbs = (prep.get("source") or {}).get("databases") or {}
        have = ident.get("userVersions") or {}
        bad = [db for db in DATABASES
               if (dbs.get(db) or {}).get("userVersion") is None
               or (dbs.get(db) or {}).get("userVersion") != have.get(db)]
        check("preparation.userVersions", not bad, ", ".join(
            f"{db} prepared at {(dbs.get(db) or {}).get('userVersion')}, "
            f"clone at {have.get(db)}" for db in bad))

    for phase in PASSES:
        p = (passes or {}).get(phase) or {}
        error = f" ({p.get('error')})" if p.get("error") else ""
        check(f"{phase}.exit", p.get("exit") == 0,
              f"cache-sync exited {p.get('exit')}{error}")
        raw = p.get("stats")
        check(f"{phase}.stats", isinstance(raw, dict),
              "no sync_cache statistics were captured")
        stats = raw if isinstance(raw, dict) else {}
        dirty = [f for f in COMMON_CLEAN_FIELDS if stats.get(f)]
        check(f"{phase}.commonClean", isinstance(raw, dict) and not dirty,
              ("dirty: " + ", ".join(f"{f}={stats.get(f)!r}" for f in dirty))
              if dirty else "no statistics")
        counts = [stats.get(k) for k in ("files_processed",
                                         "files_skipped_unchanged",
                                         "files_total")]
        complete = (all(isinstance(n, int) for n in counts)
                    and counts[0] + counts[1] == counts[2])
        check(f"{phase}.census", complete,
              "files_processed + files_skipped_unchanged != files_total "
              f"({counts[0]} + {counts[1]} vs {counts[2]})")
        claude = p.get("claude")
        claude = claude if isinstance(claude, dict) else None
        obs = claude or {}
        check(f"{phase}.dedupApplied", obs.get("dedupApplied") is True,
              "cache migration 001_dedup_highest_wins was not applied before "
              f"the pass (observed {obs.get('dedupApplied')!r})")
        found = obs.get("discovery")
        check(f"{phase}.discovery",
              isinstance(found, dict) and found.get("invocations", 0) >= 1
              and found.get("complete") is True and not found.get("errors"),
              "the walk's own discovery was not observed completely"
              + (f": {found.get('errors')}" if isinstance(found, dict)
                 and found.get("errors") else ""))
        check(f"{phase}.snapshot",
              claude is not None and not obs.get("snapshotErrors"),
              f"session_files unreadable: {obs.get('snapshotErrors')}")
        check(f"{phase}.absence",
              claude is not None and not obs.get("absenceErrors"),
              f"absence unconfirmed: {obs.get('absenceErrors')}")
        differ = []
        for name in RESIDUAL_SETS:
            got = obs.get(name)
            if residual is None or not isinstance(got, list) \
                    or sorted(got) != residual:
                added = sorted(set(got or []) - set(residual or []))
                removed = sorted(set(residual or []) - set(got or []))
                differ.append(f"{name} " + (
                    "not observed" if not isinstance(got, list) else
                    f"+{added} -{removed}"))
        check(f"{phase}.residual", not differ,
              ("no preparation residual to compare; " if residual is None
               else "") + "; ".join(differ))
    verify = ((passes or {}).get("verify") or {}).get("stats")
    processed = verify.get("files_processed") if isinstance(verify, dict) \
        else None
    if justified_claude is None:
        check("verify.claudeProcessed", processed == 0,
              f"verification processed {processed} Claude file(s); it must "
              "process none")
    else:
        # Revision 15 (Q16, finite-frontier admission): a live verification
        # may process exactly the files per-file evidence shows grew beyond
        # the frontier or appeared after it - never more.
        check("verify.claudeProcessed",
              isinstance(processed, int) and processed <= justified_claude,
              f"verification processed {processed} Claude file(s); per-file "
              f"evidence beyond the frontier justifies {justified_claude}")

    pr = prune if isinstance(prune, dict) else {}
    raw = pr.get("result")
    check("prune.present", pr.get("ran") is True and isinstance(raw, dict),
          "the tested tree's --prune-orphans helper returned no structured "
          "result" + (f" ({pr.get('error')})" if pr.get("error") else ""))
    result = raw if isinstance(raw, dict) else {}
    check("prune.exit", pr.get("exit") == 0,
          f"cache-sync --prune-orphans exited {pr.get('exit')}")
    counts = {k: result.get(k) for k in ("pruned_files", "pruned_entries",
                                         "pruned_messages")}
    check("prune.mutation", all(v == 0 for v in counts.values()),
          ", ".join(f"{k}={v}" for k, v in counts.items()))
    check("prune.contention", result.get("contended") is False,
          f"contended={result.get('contended')!r}")
    check("prune.refusal", result.get("prune_refused") is False,
          f"prune_refused={result.get('prune_refused')!r}")
    paths = result.get("residual_paths")
    check("prune.residual", residual is not None and isinstance(paths, list)
          and sorted(paths) == residual,
          f"residual_paths {paths} vs preparation residual {residual}")
    return checks


def claude_residual_admission(prep, passes, prune, *, identity=None,
                              justified_claude=None) -> "list[str]":
    """Spec §6.3 revision 10: the problems that keep a Claude walk whose only
    uncertifiable cause should be a stable, prepared orphan residual from
    admission; an empty list admits it. Used ONLY as the Claude alternative
    when `provider_sync_certifiable("full", …)` fails. `justified_claude`
    (live finite-frontier admission only) is the number of Claude files the
    frontier evidence lets verification process; None keeps "none"."""
    return [f"{c['check']}: {c['detail']}"
            for c in residual_exception_checks(prep, passes, prune,
                                               identity=identity,
                                               justified_claude=justified_claude)
            if not c["ok"]]


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
                      now: dict) -> "tuple[list[str], dict]":
    """Pure. The catch-up must consume the frontier in every store that tracks
    a file; verification may process a file only where per-file evidence
    (`now`, read after it) shows bytes beyond the frontier or a file first
    discovered after it; a cursor reset, a replay, a prune or a live identity
    change is never admitted. Returns (problems, {provider: justified files})."""
    problems = []
    files = frontier.get("files") or {}
    for path, f in sorted(files.items()):
        cur = now.get(path)
        if cur is None or (cur["dev"], cur["ino"]) != (f["dev"], f["ino"]) \
                or cur["size"] < f["size"]:
            problems.append(f"{path}: an unexplained discontinuity after the "
                            "frontier (replaced, shrunk or gone)")
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


def admission_problems(frontier, final: dict, gaps, ingest, *,
                       claude_exception=None) -> "list[str]":
    """`claude_exception`: None when the exception was not evaluated, [] when
    it admits the Claude walk, else its problems. Codex accounting and both
    conversation censuses always keep their ordinary predicates."""
    problems = []
    if not frontier.provider_sync_certifiable("full", final.get("sync_cache")):
        if claude_exception is None:
            problems.append("sync_cache is not certifiable as a full walk")
        elif claude_exception:
            problems.append(
                "sync_cache is not certifiable as a full walk, and the "
                "stable-residual exception does not apply: "
                + "; ".join(claude_exception))
    if not frontier.provider_sync_certifiable(
            "full", final.get("sync_codex_cache")):
        problems.append("sync_codex_cache is not certifiable as a full walk")
    for name in ("sync_claude_conversations", "sync_codex_conversations"):
        if not frontier.conversation_sync_certifiable("full", final.get(name)):
            problems.append(f"{name} is not certifiable as a full census")
    problems.extend(gaps)
    if ingest is None or not ingest.get("ran") or ingest.get("consumed") \
            or ingest.get("error"):
        problems.append(f"the stats journal is not drained ({ingest})")
    return problems


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

def _prune_pass(c, capture) -> dict:
    """The tested tree's `cache-sync --prune-orphans` on this clone, with the
    helper's structured `PruneResult` (an exit code alone is not evidence)."""
    capture.prunes.clear()
    args = argparse.Namespace(source="all", rebuild=False, prune_orphans=True,
                              prune_conversations=False)
    t0 = time.monotonic()
    try:
        code, error = c.cmd_cache_sync(args), None
    except Exception as exc:  # noqa: BLE001 - kept as evidence
        code, error = None, f"{type(exc).__name__}: {exc}"
    return {"ran": True, "exit": code, "error": error,
            "calls": len(capture.prunes),
            "seconds": round(time.monotonic() - t0, 3),
            "result": _wire(capture.prunes[-1]) if capture.prunes else None}


def _input_mode() -> "tuple[str | None, str | None]":
    """(mode, freeze): ("frozen", FREEZE), ("live", None), or (None, None)
    when no input mode is set (the pre-revision-15 behaviour, kept for the
    hermetic tests; every runner sets one)."""
    raw = os.environ.get("WRITE_ATTRIBUTION_INPUTS", "")
    if raw.startswith("frozen:"):
        return "frozen", raw[len("frozen:"):]
    return ("live", None) if raw == "live" else (None, None)


def run(tree: pathlib.Path, deadline_s: float, *, source=None,
        prep_path=None, out=None) -> "tuple[int, dict]":
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
    phases, certification = {}, {}
    for phase in PASSES:
        t0 = time.monotonic()
        try:
            code, error = c.cmd_cache_sync(args), None
        except Exception as exc:  # noqa: BLE001 - kept as evidence
            code, error = None, f"{type(exc).__name__}: {exc}"
        cycles = []
        while time.monotonic() - started < deadline_s:
            result = journal.run_stats_ingest(mode="authoritative",
                                              timeout_s=60.0)
            cycles.append({"ran": result.ran, "consumed": result.consumed,
                           "malformed": result.malformed,
                           "error": None if result.error is None
                           else repr(result.error)})
            if not result.ran or not result.consumed or result.error:
                break
        phases[phase] = {
            "exit": code, "error": error,
            "seconds": round(time.monotonic() - t0, 3),
            "syncs": {name: _wire(capture.last(name))
                      for name in Capture.NAMES},
            "claude": capture.last_claude(),
            "ingestCycles": cycles,
        }
        # The original certification results, kept whatever route admits.
        certification[phase] = {
            name: bool(frontier.provider_sync_certifiable(
                "full", capture.last(name)))
            for name in ("sync_cache", "sync_codex_cache")}
        certification[phase].update({
            name: bool(frontier.conversation_sync_certifiable(
                "full", capture.last(name)))
            for name in ("sync_claude_conversations",
                         "sync_codex_conversations")})
        if phase == "catchUp":
            capture.calls.clear()
            after_catchup = retained_state(data_dir)
    final = {name: capture.last(name) for name in Capture.NAMES}
    after_verify = retained_state(data_dir)
    frontier_receipt, mode_problems, justified = None, [], None
    if mode == "frozen":
        relocation = relocation_problems(frozen["before"], after_catchup,
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
            # HR-12: the verification pass against the catch-up's result
            "verification": {"checked": True, "problems": verification}}
    elif mode == "live":
        now = current_files(sorted(set(live_frontier["files"]) | {
            row["path"] for row in after_verify["rows"].values()}))
        mode_problems, justified = frontier_problems(
            live_frontier, after_catchup, after_verify, now)
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
    ordinary = bool(frontier.provider_sync_certifiable(
        "full", final.get("sync_cache")))
    prune, checks, claude_exception = {"ran": False}, [], None
    if not ordinary and source_root is not None:
        if time.monotonic() - started < deadline_s:
            prune = _prune_pass(c, capture)
        passes = {phase: {"exit": phases[phase]["exit"],
                          "error": phases[phase]["error"],
                          "stats": phases[phase]["syncs"]["sync_cache"],
                          "claude": phases[phase]["claude"]}
                  for phase in PASSES}
        checks = residual_exception_checks(
            prep, passes, prune, identity=identity,
            justified_claude=None if justified is None else justified["claude"])
        claude_exception = [f"{c_['check']}: {c_['detail']}" for c_ in checks
                            if not c_["ok"]]
    try:
        pending = pending_state(data_dir,
                                **tree_pending_inputs(c, cache, frontier))
    except Exception as exc:  # noqa: BLE001 - missing evidence is INVALID
        pending = {"migrations": {}, "maintenanceFlags": {},
                   "stats": {"error": None, "epoch": 0, "userVersion": 0},
                   "errors": [f"pending state unreadable: "
                              f"{type(exc).__name__}: {exc}"]}
    pending_issues = pending_problems(pending)
    gaps = cursor_gaps(data_dir, marks)
    last_ingest = (phases["verify"]["ingestCycles"] or [None])[-1]
    problems = admission_problems(frontier, final, gaps, last_ingest,
                                  claude_exception=claude_exception)
    for phase in PASSES:
        if phases[phase]["exit"] != 0:
            error = phases[phase]["error"]
            problems.append(f"the {phase} cache-sync exited "
                            f"{phases[phase]['exit']}"
                            + (f" ({error})" if error else ""))
    problems.extend(pending_issues)
    problems.extend(mode_problems)
    elapsed = time.monotonic() - started
    if elapsed > deadline_s:
        problems.append(f"the drain took {elapsed:.0f} s, past the "
                        f"{deadline_s:.0f} s admission deadline")
    after_migrations = applied_migrations(data_dir)
    route = ("ordinary" if ordinary else
             EXCEPTION_ROUTE if claude_exception == [] else "refused")
    receipt.update({
        "inputMode": mode, "frontier": frontier_receipt,
        "phases": phases, "elapsedS": round(elapsed, 3),
        "pendingWork": gaps,
        "pendingState": {**pending, "problems": pending_issues},
        "certification": certification,
        "prune": prune,
        "claudeAdmission": {
            "route": route, "ordinary": ordinary,
            "exception": {"evaluated": claude_exception is not None,
                          "admitted": claude_exception == [],
                          "checks": checks,
                          "problems": claude_exception or []}},
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
                        help="SRC_ROOT the clone was made from; enables the "
                             "Claude stable-residual exception")
    parser.add_argument("--prep", type=pathlib.Path,
                        help=f"default: SRC_ROOT/{PREP_RECEIPT}")
    args = parser.parse_args(argv)
    code, receipt = run(args.tree.resolve(), args.deadline_s,
                        source=args.source, prep_path=args.prep, out=args.out)
    args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True,
                                   default=str) + "\n")
    print("ADMITTED" + (f" (Claude: {EXCEPTION_ROUTE})"
                        if receipt["claudeAdmission"]["route"]
                        == EXCEPTION_ROUTE else "")
          if code == 0 else "INVALID: " + "; ".join(receipt["problems"]))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
