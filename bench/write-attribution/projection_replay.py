"""#901 spec §6.3 P (Q12, `dc9` G3): the projection replay.

usage:
  projection_replay.py p1 --tree TREE --db CLONE --conversation KEY
                          [--late-anchor] [--reps 5] [--role candidate|control]
                          [--dylib DYLIB] [--real-codex-home ROOTS] --out JSON
  projection_replay.py p1-verdict RECEIPT...   (each beside its run's inputs.json)
  projection_replay.py p2-extract --window START END --out DIR [--codex-home ROOTS]
  projection_replay.py p2-place --slice DIR --root ROOT
  projection_replay.py p2-replay --slice DIR --root ROOT --seconds S [--time-scale X]

P1, per conversation (on its own APFS clone of the B source copy; `run-p1.sh`
builds the clone, the interposer and its self-test receipt). CLONE is the clone's
data directory (or its conversations.db). The retained conversation KEY's rollout
files are copied, read-only, into a scratch root beside the clone
(`CLONE/../p1-scratch/codex`) and ingested once as setup, so the measured
conversation has the retained one's records under the scratch root; every pass
reads the real roots read-only as well (`--real-codex-home`, default
`$CODEX_HOME` or `~/.codex`), exactly as B does, because a pass whose
`CODEX_HOME` omitted them would prune their whole history from the clone. Each
repetition then runs, each in its own process under the interposer:
  forced       the tree's `materialize_codex_find_projection` on the unchanged
               conversation, under the provider flock, then COMMIT;
  no-append    one ordinary `sync_codex_conversations` pass with nothing new;
  append       a small append to the scratch copy (a turn context, a user message,
               a reasoning item, a tool call with its output and a token count; a
               few KB), the Codex cache-sync as setup, then one complete pass;
  late-anchor  (--late-anchor) a resumed segment without a turn context ingested
               as setup, then a measured append of a user message timestamped
               before that segment (an ordinal shift) and a native completion
               carrying a turn id (the late anchor), followed by a comparison with
               a from-zero rebuild of the scratch root in a fresh data directory.
Every measured process starts from a `wal_checkpoint(TRUNCATE)` taken outside
it, runs with `wal_autocheckpoint = 0`, and ends with its own forced
`wal_checkpoint(TRUNCATE)`. Recorded per pass: projection rows deleted, inserted
and updated for the conversation (TEMP triggers on the measuring connection, so
cascades count) and for every conversation (ambient detection), the generation
before and after, the interposer's WAL, checkpoint-copy, temp and other bytes,
whole-pass wall and CPU time, the projection's construction, comparison and
commit time (timed from the harness around the tree's own functions and
statements; the tree exposes nothing new), the provider flock's hold, the peak
footprint (`rusage.py`) and the files the pass processed. A missing field makes
the receipt invalid; `p1-verdict` judges the receipts of both conversations for
the candidate and for the control (the candidate before revision 11).

Revision 13 (Q14, `dc11` J5) keeps the live roots and the ambient refusal and
adds one repetition rule: a measured pass refused for ambient activity is
discarded and run again, at most three more times per case (each case's three
are shared across its repetitions). The receipt's `refusedRepetitions` lists
every discarded pass with its reason; `p1-verdict` still refuses a case
without five ambient-free repetitions, and one that took more than three.

P2: `p2-extract` reads rollout files read-only and keeps, per file, the lines
whose timestamps fall in [START, END] with their times relative to START, plus
the file truncated at the window start; `p2-place` writes those prefixes into the
clone's scratch root and `p2-replay` re-appends the lines at their original
relative times (`run-p2.sh` drives both inside B's setup and measured window).
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import importlib.machinery
import importlib.util
import json
import math
import os
import pathlib
import pwd
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
UTC = dt.timezone.utc
SCHEMA = "p1/1"
CASES = ("forced", "no-append", "append", "late-anchor")
#: Every field a measured pass must carry (spec §6.3 P: "A receipt missing any
#: of these fields is invalid").
REQUIRED_PASS_FIELDS = (
    "case", "rep", "rowsDeleted", "rowsInserted", "rowsUpdated",
    "generationBefore", "generationAfter", "walBytes", "checkpointBytes",
    "tempBytes", "wallS", "cpuS", "constructionS", "comparisonS", "commitS",
    "flockHoldS", "footprintPeakBytes", "filesProcessed", "ambient",
)
TWO_SIZE_BYTES_RATIO = 1.25
_PROJECTION_WRITE = re.compile(
    r"^(INSERT|UPDATE|DELETE)\b.*\bcodex_find_projection\b"
    r"|codex_find_projection_generation", re.IGNORECASE)


# ── shared helpers ───────────────────────────────────────────────────────────


def _now_iso(at: "float | None" = None) -> str:
    moment = dt.datetime.fromtimestamp(at, UTC) if at else dt.datetime.now(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_instant(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _line_instant(raw: bytes) -> "float | None":
    try:
        stamp = json.loads(raw).get("timestamp")
        return _parse_instant(stamp) if isinstance(stamp, str) else None
    except (ValueError, TypeError, AttributeError):
        return None


def percentile(values, q):
    """The soak's nearest-rank percentile (bench/dashboard-soak.py)."""
    values = sorted(float(v) for v in values)
    if not values:
        return None
    return values[max(0, min(len(values) - 1, math.ceil(q * len(values)) - 1))]


def _production_dir() -> pathlib.Path:
    return pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share" / "cctally"


def _refuse_production(path: pathlib.Path) -> None:
    resolved = path.resolve()
    prod = _production_dir().resolve()
    if resolved == prod or prod in resolved.parents:
        raise SystemExit(f"refusing to measure the production data directory: {resolved}")


def _inside(root: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    resolved = pathlib.Path(path).resolve()
    if pathlib.Path(root).resolve() not in resolved.parents:
        raise SystemExit(f"refusing to write outside the clone: {resolved}")
    return resolved


def _ro(path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _codex_roots(spec: "str | None") -> list[pathlib.Path]:
    raw = spec if spec is not None else os.environ.get("CODEX_HOME") or str(
        pathlib.Path.home() / ".codex")
    return [pathlib.Path(p).expanduser() for p in raw.split(",") if p.strip()]


def compose_libraries(existing: "str | None", lib: str) -> str:
    """`existing` + `lib` as a DYLD_INSERT_LIBRARIES list, `lib` once."""
    libs = [x for x in (existing or "").split(":") if x]
    if lib not in libs:
        libs.append(lib)
    return ":".join(libs)


def _walk_root(root: pathlib.Path) -> pathlib.Path:
    """The product walks `ROOT/sessions` when it exists, else ROOT itself."""
    sessions = root / "sessions"
    return sessions if sessions.is_dir() else root


# ── the measured process (`_pass`, `_rebuild`) ───────────────────────────────


class _Clock:
    """Harness-side timing of the tree's projection work (no product hook)."""

    def __init__(self) -> None:
        self.projection_depth = 0
        self.difference_depth = 0
        self.await_commit = False
        self.t = {"projectionTotalS": 0.0, "differenceS": 0.0,
                  "writeInDifferenceS": 0.0, "writeOutsideDifferenceS": 0.0,
                  "projectionCommitS": 0.0, "otherCommitS": 0.0}
        self.locks: list[tuple[float, str, int]] = []


CLOCK = _Clock()


def _install_instruments() -> None:
    """Hand every connection a timing subclass (as the #901 G1 plan guard
    does) and record every successful flock call, before the tree loads."""
    import fcntl

    real_connect = sqlite3.connect

    class TimingConnection(sqlite3.Connection):
        def _timed(self, method, sql, args):
            write = (CLOCK.projection_depth
                     and _PROJECTION_WRITE.search(" ".join(str(sql).split())))
            if not write:
                return method(self, sql, *args)
            started = time.perf_counter()
            try:
                return method(self, sql, *args)
            finally:
                key = ("writeInDifferenceS" if CLOCK.difference_depth
                       else "writeOutsideDifferenceS")
                CLOCK.t[key] += time.perf_counter() - started

        def execute(self, sql, *args):
            return self._timed(sqlite3.Connection.execute, sql, args)

        def executemany(self, sql, *args):
            return self._timed(sqlite3.Connection.executemany, sql, args)

        def commit(self):
            started = time.perf_counter()
            try:
                return super().commit()
            finally:
                elapsed = time.perf_counter() - started
                if CLOCK.await_commit:
                    CLOCK.t["projectionCommitS"] += elapsed
                    CLOCK.await_commit = False
                else:
                    CLOCK.t["otherCommitS"] += elapsed

    def connect(*args, **kwargs):
        if "factory" in kwargs or len(args) > 5:
            return real_connect(*args, **kwargs)
        kwargs["factory"] = TimingConnection
        return real_connect(*args, **kwargs)

    sqlite3.connect = connect
    real_flock = fcntl.flock

    def flock(fd, operation):
        result = real_flock(fd, operation)
        name = getattr(fd, "name", None)
        if isinstance(name, os.PathLike):
            name = os.fspath(name)
        if isinstance(name, str):
            CLOCK.locks.append((time.perf_counter(), os.path.realpath(name),
                                operation))
        return result

    fcntl.flock = flock


def _load_tree(tree: pathlib.Path):
    """Load TREE/bin/cctally as `cctally`, exactly as the product runs."""
    sys.path.insert(0, str(tree / "bin"))
    loader = importlib.machinery.SourceFileLoader(
        "cctally", str(tree / "bin" / "cctally"))
    spec = importlib.util.spec_from_loader("cctally", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = module
    loader.exec_module(module)
    return module


def _wrap_projection(query) -> None:
    real = query.materialize_codex_find_projection

    def materialize(*args, **kwargs):
        CLOCK.projection_depth += 1
        started = time.perf_counter()
        try:
            return real(*args, **kwargs)
        finally:
            CLOCK.t["projectionTotalS"] += time.perf_counter() - started
            CLOCK.projection_depth -= 1
            CLOCK.await_commit = True

    query.materialize_codex_find_projection = materialize
    difference = getattr(query, "_write_codex_find_projection_difference", None)
    if difference is None:
        return                           # the control: no comparison phase

    def timed_difference(*args, **kwargs):
        CLOCK.difference_depth += 1
        started = time.perf_counter()
        try:
            return difference(*args, **kwargs)
        finally:
            CLOCK.t["differenceS"] += time.perf_counter() - started
            CLOCK.difference_depth -= 1

    query._write_codex_find_projection_difference = timed_difference


_COUNTERS = """
CREATE TEMP TABLE IF NOT EXISTS p1_ops(op TEXT NOT NULL, conversation_key TEXT);
CREATE TEMP TRIGGER IF NOT EXISTS p1_ai AFTER INSERT ON codex_find_projection
BEGIN INSERT INTO p1_ops VALUES ('insert', new.conversation_key); END;
CREATE TEMP TRIGGER IF NOT EXISTS p1_au AFTER UPDATE ON codex_find_projection
BEGIN INSERT INTO p1_ops VALUES ('update', new.conversation_key); END;
CREATE TEMP TRIGGER IF NOT EXISTS p1_ad AFTER DELETE ON codex_find_projection
BEGIN INSERT INTO p1_ops VALUES ('delete', old.conversation_key); END;
"""


def _generation(conn) -> int:
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key='codex_find_projection_generation'"
    ).fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _ops(conn, key: "str | None") -> dict:
    sql = "SELECT op, COUNT(*) FROM p1_ops"
    args: tuple = ()
    if key is not None:
        sql += " WHERE conversation_key=?"
        args = (key,)
    counts = dict(conn.execute(sql + " GROUP BY op", args).fetchall())
    return {"deleted": counts.get("delete", 0), "inserted": counts.get("insert", 0),
            "updated": counts.get("update", 0)}


def _footprint_peak(pid: int) -> "int | None":
    env = {k: v for k, v in os.environ.items()
           if k not in ("DYLD_INSERT_LIBRARIES", "WTRACE_OUT")}
    out = subprocess.run([sys.executable, str(HERE / "rusage.py"), str(pid)],
                         capture_output=True, text=True, env=env)
    try:
        return json.loads(out.stdout.splitlines()[-1]).get("footprint_peak")
    except (ValueError, IndexError):
        return None


def _flock_hold(lock_path: str) -> "float | None":
    import fcntl

    target = os.path.realpath(lock_path)
    acquired = None
    hold = None
    for at, name, operation in CLOCK.locks:
        if name != target:
            continue
        if operation & fcntl.LOCK_EX:
            acquired = at
        elif operation & fcntl.LOCK_UN and acquired is not None:
            hold = (hold or 0.0) + (at - acquired)
            acquired = None
    return hold


def physical_rows(conn, key: str) -> list:
    """Every projection column, `message_id` replaced by its physical position."""
    return sorted(list(row) for row in conn.execute(
        "SELECT m.source_path, m.line_offset, p.surface, p.conversation_key, "
        "p.item_key, p.block_key, p.container_block_key, p.render_order, "
        "p.projected_text, p.leaves_json, p.disclosure_json, p.projection_version "
        "FROM codex_find_projection p "
        "JOIN codex_conversation_messages m ON m.id = p.message_id "
        "WHERE p.conversation_key=?", (key,)))


def _stats_dict(stats) -> dict:
    return {name: getattr(stats, name) for name in (
        "files_total", "files_processed", "files_skipped_unchanged",
        "files_failed", "files_pruned", "files_reset_truncated",
        "lock_contended", "deferred_reason", "prune_refused")
        if hasattr(stats, name)}


def run_pass(args) -> int:
    """One measured (or setup) step in its own process."""
    _install_instruments()
    tree = pathlib.Path(args.tree).resolve()
    c = _load_tree(tree)
    import _lib_codex_conversation_query as query
    import _cctally_core as core

    _wrap_projection(query)
    result: dict = {"pid": os.getpid(), "case": args.case, "step": args.step}
    if args.step == "cache":
        conn = c.open_cache_db()
        try:
            started = time.perf_counter()
            stats = c.sync_codex_cache(conn)
            result["wallS"] = time.perf_counter() - started
            result["stats"] = _stats_dict(stats)
        finally:
            conn.close()
    elif args.step == "export":
        conn = c.open_conversations_db()
        try:
            result["rows"] = physical_rows(conn, args.conversation)
        finally:
            conn.close()
    else:
        conn = c.open_conversations_db()
        try:
            conn.executescript(_COUNTERS)
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.commit()
            before = _generation(conn)
            lock_path = str(core.CONVERSATIONS_LOCK_CODEX_PATH)
            cpu0, t0 = time.process_time(), time.perf_counter()
            result["startedAt"] = time.time()
            if args.step == "forced":
                cache = sys.modules.get("_cctally_cache") or c._load_sibling(
                    "_cctally_cache")
                core.APP_DIR.mkdir(parents=True, exist_ok=True)
                pathlib.Path(lock_path).touch()
                with open(lock_path, "w") as lock_fh:
                    if not cache._acquire_cache_flock(lock_fh, timeout=120.0):
                        raise SystemExit("the provider flock was not acquired")
                    try:
                        query.materialize_codex_find_projection(
                            conn, [args.conversation])
                        conn.commit()
                    finally:
                        import fcntl
                        fcntl.flock(lock_fh, fcntl.LOCK_UN)
                result["stats"] = {"files_processed": 0}
            else:
                stats = c.sync_codex_conversations(conn)
                result["stats"] = _stats_dict(stats)
            result["wallS"] = time.perf_counter() - t0
            result["cpuS"] = time.process_time() - cpu0
            result["endedAt"] = time.time()
            result["generationBefore"] = before
            result["generationAfter"] = _generation(conn)
            result["ops"] = _ops(conn, args.conversation)
            result["opsAll"] = _ops(conn, None)
            if args.conversation:
                result["messages"] = conn.execute(
                    "SELECT COUNT(*) FROM codex_conversation_messages "
                    "WHERE conversation_key=?", (args.conversation,)).fetchone()[0]
                result["projectionRows"] = conn.execute(
                    "SELECT COUNT(*) FROM codex_find_projection "
                    "WHERE conversation_key=?", (args.conversation,)).fetchone()[0]
            conn.commit()
            result["checkpoint"] = list(conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            result["flockHoldS"] = _flock_hold(lock_path)
        finally:
            conn.close()
    t = CLOCK.t
    result["timing"] = dict(t)
    result["constructionS"] = max(0.0, t["projectionTotalS"] - t["differenceS"]
                                  - t["writeOutsideDifferenceS"])
    result["comparisonS"] = max(0.0, t["differenceS"] - t["writeInDifferenceS"])
    result["commitS"] = (t["writeInDifferenceS"] + t["writeOutsideDifferenceS"]
                         + t["projectionCommitS"])
    result["footprintPeakBytes"] = _footprint_peak(os.getpid())
    pathlib.Path(args.result).write_text(json.dumps(result, default=str) + "\n")
    return 0


# ── P1 orchestration ─────────────────────────────────────────────────────────


def _data_dir(db: str) -> pathlib.Path:
    path = pathlib.Path(db).resolve()
    return path.parent if path.name == "conversations.db" else path


def _checkpoint(data: pathlib.Path) -> None:
    """Truncate the WAL outside the measured process."""
    conn = sqlite3.connect(str(data / "conversations.db"))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()


def interposer_bytes(prefix: pathlib.Path, pid: int) -> "dict | None":
    """{wal, db, temp, cache, other, dropped} from a process's exit snapshot;
    None when there is none (no interposer)."""
    path = pathlib.Path(f"{prefix}.{pid}.exit")
    if not path.exists():
        return None
    lines = [json.loads(line) for line in path.read_text().splitlines()
             if line.strip()]
    if not lines:
        return None
    snap = lines[-1]
    out = {"wal": 0, "db": 0, "temp": 0, "cache": 0, "other": 0,
           "dropped": int(snap.get("dropped", 0) or 0)
           + int(snap.get("framesDropped", 0) or 0)}
    for name, (written, _calls) in (snap.get("paths") or {}).items():
        base = os.path.basename(name)
        if base == "conversations.db":
            out["db"] += written
        elif base == "conversations.db-wal":
            out["wal"] += written
        elif base.startswith("etilqs_"):
            out["temp"] += written
        elif base.startswith("cache.db"):
            out["cache"] += written
        else:
            out["other"] += written
    return out


class P1:
    def __init__(self, args) -> None:
        self.args = args
        self.tree = pathlib.Path(args.tree).resolve()
        self.data = _data_dir(args.db)
        _refuse_production(self.data)
        if not (self.data / "conversations.db").exists():
            raise SystemExit(f"no conversations.db in {self.data}")
        self.clone = self.data.parent
        self.scratch = self.clone / "p1-scratch" / "codex"
        if self.scratch.exists():
            raise SystemExit(f"{self.scratch} exists; run P1 on a fresh clone")
        self.out = pathlib.Path(args.out).resolve()
        self.work = self.out.with_suffix(".d")
        self.work.mkdir(parents=True, exist_ok=True)
        self.real_roots = _codex_roots(args.real_codex_home)
        for root in self.real_roots:
            if self.clone.resolve() in root.resolve().parents:
                raise SystemExit("a real root may not live inside the clone")
        self.env = {**os.environ, "CCTALLY_DATA_DIR": str(self.data),
                    "CODEX_HOME": ",".join(
                        [str(r) for r in self.real_roots] + [str(self.scratch)]),
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.setup: list[dict] = []
        self.passes: list[dict] = []
        #: Repetitions refused for ambient activity and run again (rev. 13).
        self.refused: list[dict] = []
        self.rebuilds: list[dict] = []
        self.files: list[pathlib.Path] = []
        self.tail: "pathlib.Path | None" = None
        self.last_at = 0.0
        self.total_tokens = 0
        self.meta: dict = {}
        self.model = "gpt-5"

    # -- subprocesses ------------------------------------------------------------

    def _step(self, step: str, *, case: str, rep: int, measured: bool,
              env: "dict | None" = None, conversation: str = "") -> dict:
        tag = f"{case}-{rep}-{step}"
        result_path = self.work / f"{tag}.json"
        child_env = dict(env or self.env)
        prefix = self.work / f"wtrace-{tag}"
        if measured and self.args.dylib:
            # Revision 15 (Q16): APPEND the write interposer to the libraries
            # the parent was launched with (rootmap.dylib in a frozen run);
            # replacing the list lost the namespace in every measured child.
            child_env.update({"DYLD_INSERT_LIBRARIES": compose_libraries(
                                  child_env.get("DYLD_INSERT_LIBRARIES"),
                                  self.args.dylib),
                              "WTRACE_OUT": str(prefix), "WTRACE_PERIOD": "5"})
        argv = [sys.executable, str(pathlib.Path(__file__).resolve()), "_pass",
                "--tree", str(self.tree), "--step", step, "--case", case,
                "--conversation", conversation, "--result", str(result_path)]
        child = subprocess.run(argv, env=child_env, capture_output=True, text=True)
        if child.returncode != 0 or not result_path.exists():
            raise SystemExit(f"{tag} failed ({child.returncode}): "
                             f"{child.stderr[-2000:]}")
        result = json.loads(result_path.read_text())
        result["interposer"] = (interposer_bytes(prefix, result["pid"])
                                if measured and self.args.dylib else None)
        return result

    def _record(self, case: str, rep: int, result: dict, *,
                expected_files: int) -> dict:
        ib = result["interposer"]
        stats = result.get("stats") or {}
        processed = stats.get("files_processed")
        ops, ops_all = result["ops"], result["opsAll"]
        ambient = (processed is not None and processed > expected_files) or any(
            ops_all[k] != ops[k] for k in ops)
        record = {
            "case": case, "rep": rep,
            "rowsDeleted": ops["deleted"], "rowsInserted": ops["inserted"],
            "rowsUpdated": ops["updated"],
            "rowsChanged": ops["deleted"] + ops["inserted"] + ops["updated"],
            "rowsChangedAllConversations": sum(ops_all.values()),
            "generationBefore": result["generationBefore"],
            "generationAfter": result["generationAfter"],
            "walBytes": None if ib is None else ib["wal"],
            "checkpointBytes": None if ib is None else ib["db"],
            "tempBytes": None if ib is None else ib["temp"],
            "cacheBytes": None if ib is None else ib["cache"],
            "otherBytes": None if ib is None else ib["other"],
            "droppedRecords": None if ib is None else ib["dropped"],
            "wallS": result["wallS"], "cpuS": result["cpuS"],
            "constructionS": result["constructionS"],
            "comparisonS": result["comparisonS"], "commitS": result["commitS"],
            "timing": result["timing"],
            "flockHoldS": result["flockHoldS"],
            "finalCheckpoint": result.get("checkpoint"),
            "footprintPeakBytes": result["footprintPeakBytes"],
            "filesProcessed": 0 if processed is None else processed,
            "stats": stats, "ambient": ambient,
            "messages": result.get("messages"),
            "projectionRows": result.get("projectionRows"),
        }
        if ib is not None and ib["dropped"]:
            record["walBytes"] = None            # dropped records: invalid
        self.passes.append(record)
        return record

    # -- scratch conversation ------------------------------------------------------

    def _source_files(self) -> list[pathlib.Path]:
        conn = _ro(self.data / "conversations.db")
        try:
            paths = {row[0] for row in conn.execute(
                "SELECT DISTINCT source_path FROM codex_conversation_messages "
                "WHERE conversation_key=?", (self.args.conversation,))}
        finally:
            conn.close()
        if not paths:
            raise SystemExit(f"no retained messages for {self.args.conversation}")
        return sorted(pathlib.Path(p) for p in paths)

    def _scratch_target(self, source: pathlib.Path) -> pathlib.Path:
        for root in self.real_roots:
            walk = _walk_root(root).resolve()
            if walk in source.resolve().parents:
                rel = source.resolve().relative_to(walk)
                return self.scratch / "sessions" / rel
        return self.scratch / "sessions" / "p1" / source.name

    def copy_conversation(self) -> None:
        for source in self._source_files():
            if not source.is_file():
                raise SystemExit(f"the retained rollout is missing: {source}")
            target = _inside(self.clone, self._scratch_target(source))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            self.files.append(target)

    def resolve_conversation(self) -> str:
        conn = _ro(self.data / "conversations.db")
        try:
            keys = {}
            for path in self.files:
                for key, count in conn.execute(
                        "SELECT conversation_key, COUNT(*) FROM "
                        "codex_conversation_messages WHERE source_path=? "
                        "GROUP BY conversation_key", (str(path),)):
                    keys[key] = keys.get(key, 0) + count
            if not keys:
                raise SystemExit("the scratch copy ingested no messages")
            key = max(keys, key=keys.get)
            row = conn.execute(
                "SELECT source_path, MAX(timestamp_utc) FROM "
                "codex_conversation_messages WHERE conversation_key=?",
                (key,)).fetchone()
        finally:
            conn.close()
        self.tail = pathlib.Path(row[0])
        self.last_at = _parse_instant(row[1]) if row[1] else time.time()
        self._read_tail()
        return key

    def _read_tail(self) -> None:
        with open(self.tail, "rb") as fh:
            for raw in fh:
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                kind = record.get("type")
                payload = record.get("payload") or {}
                if kind == "session_meta" and not self.meta:
                    self.meta = payload
                if kind == "turn_context" and isinstance(payload.get("model"), str):
                    self.model = payload["model"]
                info = payload.get("info") if isinstance(payload, dict) else None
                if isinstance(info, dict):
                    total = (info.get("total_token_usage") or {}).get("total_tokens")
                    if isinstance(total, int):
                        self.total_tokens = total
                at = record.get("timestamp")
                if isinstance(at, str):
                    try:
                        self.last_at = max(self.last_at, _parse_instant(at))
                    except ValueError:
                        pass
        if not self.meta:
            raise SystemExit(f"{self.tail} has no session_meta to continue")
        if isinstance(self.meta.get("model"), str) and self.model == "gpt-5":
            self.model = self.meta["model"]

    def _append(self, records: list[dict]) -> int:
        blob = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
        target = _inside(self.clone, self.tail)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(blob)
        return len(blob.encode("utf-8"))

    def _tick(self, seconds: float = 1.0) -> str:
        self.last_at += seconds
        return _now_iso(self.last_at)

    def small_append(self, rep: int) -> list[dict]:
        """A turn context, a user message, a reasoning item, a tool call with
        its output and a token count; a few KB, identical for both sizes."""
        turn = f"p1-turn-{rep}"
        call = f"p1-call-{rep}"
        pad = "lorem ipsum dolor sit amet " * 20
        self.total_tokens += 900
        return [
            {"type": "turn_context", "timestamp": self._tick(), "payload": {
                "model": self.model, "turn_id": turn}},
            {"type": "response_item", "timestamp": self._tick(), "payload": {
                "type": "message", "role": "user", "content": [{
                    "type": "input_text",
                    "text": f"P1 prompt {rep}: please check the projection. {pad}"}]}},
            {"type": "response_item", "timestamp": self._tick(), "payload": {
                "type": "reasoning", "summary": [{
                    "type": "summary_text", "text": f"**P1 plan {rep}** {pad}"}],
                "content": [{"type": "reasoning_text",
                             "text": f"P1 reasoning {rep} {pad}"}],
                "encrypted_content": "p1"}},
            {"type": "response_item", "timestamp": self._tick(), "payload": {
                "type": "function_call", "call_id": call, "name": "shell",
                "arguments": json.dumps({"command": ["echo", f"p1 {rep} {pad}"]})}},
            {"type": "response_item", "timestamp": self._tick(), "payload": {
                "type": "function_call_output", "call_id": call,
                "output": f"p1 output {rep} {pad}"}},
            {"type": "event_msg", "timestamp": self._tick(), "payload": {
                "type": "token_count", "info": {
                    "last_token_usage": {
                        "input_tokens": 600, "cached_input_tokens": 100,
                        "output_tokens": 300, "reasoning_output_tokens": 50,
                        "total_tokens": 900},
                    "total_token_usage": {"total_tokens": self.total_tokens}}}},
        ]

    def resumed_segment(self, rep: int) -> list[dict]:
        """A resumed segment with no turn context: its rows stay unanchored."""
        call = f"p1-resumed-call-{rep}"
        self._resume_at = self.last_at + 1.0
        return [
            {"type": "session_meta", "timestamp": self._tick(),
             "payload": dict(self.meta)},
            {"type": "response_item", "timestamp": self._tick(1.0), "payload": {
                "type": "reasoning", "summary": [{
                    "type": "summary_text", "text": f"Resumed reasoning {rep}"}],
                "content": [], "encrypted_content": "p1"}},
            {"type": "response_item", "timestamp": self._tick(), "payload": {
                "type": "function_call", "call_id": call, "name": "shell",
                "arguments": json.dumps({"command": ["echo", f"resumed {rep}"]})}},
            {"type": "response_item", "timestamp": self._tick(), "payload": {
                "type": "function_call_output", "call_id": call,
                "output": f"resumed output {rep}"}},
            {"type": "response_item", "timestamp": self._tick(), "payload": {
                "type": "message", "role": "assistant", "content": [{
                    "type": "output_text", "text": f"Resumed answer {rep}"}]}},
        ]

    def late_anchor(self, rep: int) -> list[dict]:
        """A user message timestamped before the resumed segment (every later
        ordinal shifts) and the native completion whose turn id proves it."""
        earlier = _now_iso(self._resume_at + 0.5)
        return [
            {"type": "response_item", "timestamp": earlier, "payload": {
                "type": "message", "role": "user", "content": [{
                    "type": "input_text", "text": f"Out-of-order prompt {rep}"}]}},
            {"type": "event_msg", "timestamp": self._tick(), "payload": {
                "type": "task_complete", "turn_id": f"p1-late-{rep}",
                "completed_at": _now_iso(self.last_at), "duration_ms": 1000,
                "last_agent_message": f"Resumed answer {rep}"}},
        ]

    def rebuild_equal(self, rep: int, key: str) -> dict:
        fresh = self.work / f"rebuild-{rep}"
        fresh.mkdir(parents=True)
        (fresh / "config.json").write_text(
            '{"conversation":{"retention_days":0}}\n', encoding="utf-8")
        env = {**self.env, "CCTALLY_DATA_DIR": str(fresh),
               "CODEX_HOME": str(self.scratch)}
        self._step("cache", case="rebuild", rep=rep, measured=False, env=env)
        self._step("conversations", case="rebuild", rep=rep, measured=False,
                   env=env, conversation=key)
        rebuilt = self._step("export", case="rebuild", rep=rep, measured=False,
                             env=env, conversation=key)["rows"]
        kept = self._step("export", case="late-anchor", rep=rep, measured=False,
                          conversation=key)["rows"]
        differences = [row for row in kept if row not in rebuilt] + [
            row for row in rebuilt if row not in kept]
        shutil.rmtree(fresh, ignore_errors=True)
        return {"rep": rep, "equal": kept == rebuilt, "rows": len(kept),
                "rebuiltRows": len(rebuilt), "differences": differences[:10]}

    # -- the run ------------------------------------------------------------------

    def run(self) -> dict:
        args = self.args
        catchup = [self._step("cache", case="setup", rep=0, measured=False),
                   self._step("conversations", case="setup", rep=0, measured=False)]
        self.setup.append({"catchUp": [r.get("stats") for r in catchup]})
        self.copy_conversation()
        ingest = [self._step("cache", case="setup", rep=1, measured=False),
                  self._step("conversations", case="setup", rep=1, measured=False)]
        self.setup.append({"scratchIngest": [r.get("stats") for r in ingest],
                           "files": [str(p) for p in self.files]})
        key = self.resolve_conversation()
        conn = _ro(self.data / "conversations.db")
        try:
            messages = conn.execute(
                "SELECT COUNT(*) FROM codex_conversation_messages "
                "WHERE conversation_key=?", (key,)).fetchone()[0]
            projected = conn.execute(
                "SELECT COUNT(*) FROM codex_find_projection "
                "WHERE conversation_key=?", (key,)).fetchone()[0]
            sqlite_version = conn.execute("SELECT sqlite_version()").fetchone()[0]
        finally:
            conn.close()
        budget = {case: AMBIENT_RETRIES for case in CASES}

        def measured(case, rep, run_once):
            """One repetition under the repetition rule: an ambient pass the
            case can still retry leaves the receipt's passes and is listed as
            refused; the last attempt stays, ambient or not."""
            def measure():
                record = run_once()
                if record.get("ambient") and budget.get(case, 0) > 0:
                    self.passes.remove(record)
                return record
            return measure_with_retries(case, rep, measure, budget,
                                        self.refused)

        for rep in range(1, args.reps + 1):
            def forced():
                _checkpoint(self.data)
                return self._record("forced", rep, self._step(
                    "forced", case="forced", rep=rep, measured=True,
                    conversation=key), expected_files=0)
            measured("forced", rep, forced)

            def no_append():
                _checkpoint(self.data)
                return self._record("no-append", rep, self._step(
                    "conversations", case="no-append", rep=rep, measured=True,
                    conversation=key), expected_files=0)
            measured("no-append", rep, no_append)

            def append():
                appended = self._append(self.small_append(rep))
                self._step("cache", case="append", rep=rep, measured=False)
                _checkpoint(self.data)
                record = self._record("append", rep, self._step(
                    "conversations", case="append", rep=rep, measured=True,
                    conversation=key), expected_files=1)
                record["appendedBytes"] = appended
                return record
            measured("append", rep, append)
            if args.late_anchor:
                def late():
                    self._append(self.resumed_segment(rep))
                    self._step("cache", case="late-setup", rep=rep,
                               measured=False)
                    self._step("conversations", case="late-setup", rep=rep,
                               measured=False, conversation=key)
                    appended = self._append(self.late_anchor(rep))
                    self._step("cache", case="late-anchor", rep=rep,
                               measured=False)
                    _checkpoint(self.data)
                    record = self._record("late-anchor", rep, self._step(
                        "conversations", case="late-anchor", rep=rep,
                        measured=True, conversation=key), expected_files=1)
                    record["appendedBytes"] = appended
                    check = self.rebuild_equal(rep, key)
                    record["equalsRebuild"] = check["equal"]
                    self.rebuilds.append(check)
                    return record
                measured("late-anchor", rep, late)
        rev = subprocess.run(["git", "-C", str(self.tree), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        return {
            "schema": SCHEMA, "role": args.role, "tree": str(self.tree),
            "treeRev": rev or None, "clone": str(self.data),
            "sqliteVersion": sqlite_version, "dylib": args.dylib,
            "sourceConversation": args.conversation, "conversation": key,
            "messages": messages, "projectionRows": projected,
            "lateAnchor": bool(args.late_anchor), "reps": args.reps,
            "measuredAt": _now_iso(), "realRoots": [str(r) for r in self.real_roots],
            "scratchRoot": str(self.scratch), "setup": self.setup,
            "passes": self.passes, "rebuilds": self.rebuilds,
            "refusedRepetitions": self.refused,
            "summary": summarize(self.passes),
        }


_SUMMARY_METRICS = ("rowsChanged", "walBytes", "checkpointBytes", "tempBytes",
                    "wallS", "cpuS", "constructionS", "comparisonS", "commitS",
                    "flockHoldS", "footprintPeakBytes")


def summarize(passes) -> dict:
    out = {}
    for case in CASES:
        rows = [p for p in passes if p["case"] == case]
        if not rows:
            continue
        out[case] = {"n": len(rows)}
        for metric in _SUMMARY_METRICS:
            values = [p[metric] for p in rows if p.get(metric) is not None]
            out[case][metric] = {
                "p50": percentile(values, 0.50), "p95": percentile(values, 0.95),
                "max": max(values, default=None)}
        out[case]["walPlusCheckpointBytes"] = _wpc_summary(rows)
    return out


def _wpc(record) -> "int | None":
    if record.get("walBytes") is None or record.get("checkpointBytes") is None:
        return None
    return record["walBytes"] + record["checkpointBytes"]


def _wpc_summary(rows) -> dict:
    values = [v for v in (_wpc(r) for r in rows) if v is not None]
    return {"p50": percentile(values, 0.50), "p95": percentile(values, 0.95),
            "max": max(values, default=None)}


# ── P1's repetition rule (revision 13, Q14) ─────────────────────────────────

#: How many more times a case may run a repetition P1 refused for ambient
#: activity (all of that case's repetitions together).
AMBIENT_RETRIES = 3
AMBIENT_REASON = ("ambient activity in the pass (another file or conversation "
                  "changed)")


def measure_with_retries(case: str, rep: int, measure, budget: dict,
                         refused: list) -> dict:
    """Run ``measure()`` (one measured pass returning its record) and, while
    the record is ambient and the case still has retries in ``budget``,
    discard it into ``refused`` with its reason and run it again. The last
    record is returned as measured, ambient or not: ambient detection is
    unchanged and the verdict judges it."""
    attempt = 0
    while True:
        record = measure()
        attempt += 1
        if not record.get("ambient") or budget.get(case, 0) <= 0:
            return record
        budget[case] = budget.get(case, 0) - 1
        refused.append({"case": case, "rep": rep, "attempt": attempt,
                        "reason": AMBIENT_REASON,
                        "rowsChangedAllConversations": record.get(
                            "rowsChangedAllConversations"),
                        "filesProcessed": record.get("filesProcessed")})


# ── P1 verdict ───────────────────────────────────────────────────────────────


def _receipt_problems(receipt: dict, reps: int) -> "list[str]":
    """Validity: every pass carries every field and every case its reps."""
    name = f"{receipt.get('role')}:{receipt.get('sourceConversation')}"
    problems = []
    if receipt.get("schema") != SCHEMA:
        return [f"{name}: not a {SCHEMA} receipt"]
    for record in receipt.get("passes") or ():
        missing = [f for f in REQUIRED_PASS_FIELDS if record.get(f) is None]
        if record.get("case") == "late-anchor" and record.get("equalsRebuild") is None:
            missing.append("equalsRebuild")
        if missing:
            problems.append(f"{name} {record.get('case')} rep {record.get('rep')}: "
                            f"missing {', '.join(missing)}")
        elif record["ambient"]:
            problems.append(f"{name} {record['case']} rep {record['rep']}: ambient "
                            "activity in the pass (another file or conversation "
                            "changed)")
    cases = ("forced", "no-append", "append") + (
        ("late-anchor",) if receipt.get("lateAnchor") else ())
    refused = receipt.get("refusedRepetitions") or []
    for case in cases:
        n = sum(1 for r in receipt.get("passes") or ()
                if r.get("case") == case and not r.get("ambient"))
        if n < reps:
            problems.append(f"{name}: {n} ambient-free {case} passes "
                            f"(need {reps})")
        retried = [r for r in refused if r.get("case") == case]
        if len(retried) > AMBIENT_RETRIES:
            problems.append(f"{name}: {case} ran more than three repetitions "
                            f"again ({len(retried)} refused)")
    for record in refused:
        if not record.get("reason"):
            problems.append(f"{name}: a refused {record.get('case')} "
                            f"repetition without its reason")
    return problems


def _case(receipt, case):
    return [r for r in receipt.get("passes") or () if r.get("case") == case]


def forced_noop(receipt) -> bool:
    return all(r["rowsChanged"] == 0 and r["generationAfter"] == r["generationBefore"]
               for r in _case(receipt, "forced"))


def two_size(receipts) -> "tuple[bool, dict]":
    """The small append changes the same number of projection rows on both
    conversations and its WAL-plus-checkpoint bytes differ by at most 25%."""
    counts = [sorted({r["rowsChanged"] for r in _case(x, "append")})
              for x in receipts]
    same_rows = all(len(c) == 1 for c in counts) and len(
        {c[0] for c in counts if c}) == 1
    p50 = [percentile([_wpc(r) for r in _case(x, "append")], 0.50)
           for x in receipts]
    if any(v is None for v in p50):
        ratio = None
    elif min(p50) == 0:
        ratio = 1.0 if max(p50) == 0 else math.inf
    else:
        ratio = max(p50) / min(p50)
    ok = same_rows and ratio is not None and ratio <= TWO_SIZE_BYTES_RATIO
    return ok, {"rowsChanged": counts, "walPlusCheckpointP50": p50,
                "ratio": ratio, "sameRows": same_rows}


def p1_inputs_problems(receipts: list, inputs: list) -> "list[str]":
    """Revision 15 (Q16): each receipt's run must carry valid input evidence
    (`inputs[i]` = {problems, label}); the candidate and the control must
    read one freeze (or all be live); and on frozen inputs an ambient pass,
    retried or not, means the namespace leaked."""
    invalid = []
    for receipt, inp in zip(receipts, inputs):
        name = f"{receipt.get('role')}:{receipt.get('sourceConversation')}"
        invalid.extend(f"{name}: inputs: {p}" for p in inp.get("problems") or [])
    labels = {(i["label"].get("mode"), i["label"].get("freeze")) for i in inputs}
    if len(labels) > 1:
        invalid.append("mixed input evidence in one comparison: "
                       f"{sorted(labels, key=str)} (one freeze, or all live)")
    if labels and all(mode == "frozen" for mode, _ in labels):
        for receipt in receipts:
            leaked = [p for p in receipt.get("passes") or [] if p.get("ambient")]
            leaked += receipt.get("refusedRepetitions") or []
            if leaked:
                invalid.append(
                    f"{receipt.get('role')}:{receipt.get('sourceConversation')}: "
                    f"{len(leaked)} ambient pass(es) on frozen inputs: the "
                    "namespace leaked")
    return invalid


def p1_role_problems(candidate: list, control: list) -> "list[str]":
    """Amendment 19 HR-21: a receipt's role is self-declared (`--role`), so
    it is checked against the tree each receipt recorded measuring
    (`treeRev`): every candidate receipt on one tree, every control receipt
    on one tree, and the two trees different - a mislabelled or swapped
    receipt, or one tree run as both roles, is INVALID."""
    problems = []
    revs = {}
    for group, label in ((candidate, "candidate"), (control, "control")):
        seen = {r.get("treeRev") for r in group}
        if None in seen or "" in seen:
            problems.append(f"a {label} receipt records no tree revision")
        seen.discard(None)
        seen.discard("")
        if len(seen) > 1:
            problems.append(f"the {label} receipts measured different trees "
                            f"{sorted(seen)}")
        revs[label] = seen
    if revs.get("candidate") and revs["candidate"] == revs.get("control"):
        problems.append("the candidate and the control measured the same tree "
                        f"{sorted(revs['candidate'])}")
    return problems


def p1_verdict(receipts: list, *, reps: int = 5,
               inputs: "list | None" = None) -> "tuple[int, dict]":
    """`inputs` (the CLI always passes it): per receipt, {problems, label}
    from its run directory's input-mode receipt (frozen_roots)."""
    invalid = []
    if inputs is not None:
        invalid.extend(p1_inputs_problems(receipts, inputs))
    for receipt in receipts:
        invalid.extend(_receipt_problems(receipt, reps))
    candidate = [r for r in receipts if r.get("role") == "candidate"]
    control = [r for r in receipts if r.get("role") == "control"]
    for group, label in ((candidate, "candidate"), (control, "control")):
        sources = {r.get("sourceConversation") for r in group}
        if len(group) != 2 or len(sources) != 2:
            invalid.append(f"need one {label} receipt for each of two "
                           f"conversations, have {len(group)}")
    invalid.extend(p1_role_problems(candidate, control))
    if ({r.get("sourceConversation") for r in candidate}
            != {r.get("sourceConversation") for r in control}):
        invalid.append("the control and candidate measured different conversations")
    if not any(r.get("lateAnchor") for r in candidate):
        invalid.append("no candidate receipt carries the late-anchor case")
    result: dict = {"invalid": invalid}
    if inputs is not None:
        result["inputs"] = [i["label"] for i in inputs]
    if invalid:
        return 2, result
    problems = []
    for receipt in candidate:
        name = receipt["sourceConversation"]
        if not forced_noop(receipt):
            problems.append(f"{name}: the forced re-projection of the unchanged "
                            "conversation changed rows or the generation")
        if any(r["rowsChanged"] for r in _case(receipt, "no-append")):
            problems.append(f"{name}: the no-append pass changed projection rows")
        for record in _case(receipt, "late-anchor"):
            if not record["equalsRebuild"]:
                problems.append(f"{name}: late-anchor rep {record['rep']} differs "
                                "from the from-zero rebuild")
        if any(r["tempBytes"] for r in receipt["passes"]):
            problems.append(f"{name}: temp bytes in a pass")
    ok, detail = two_size(candidate)
    result["candidateTwoSize"] = detail
    if not ok:
        problems.append(f"the small append's two-size condition fails: {detail}")
    control_two_size_ok, control_detail = two_size(control)
    result["controlTwoSize"] = control_detail
    control_problems = []
    if all(forced_noop(r) for r in control):
        control_problems.append("the control passed the forced re-projection")
    if control_two_size_ok:
        control_problems.append("the control passed the two-size condition")
    result["controlNoAppendClean"] = all(
        not r["rowsChanged"] for x in control for r in _case(x, "no-append"))
    result["summaries"] = {f"{r['role']}:{r['sourceConversation']}": {
        "messages": r.get("messages"), "projectionRows": r.get("projectionRows"),
        "summary": r.get("summary")} for r in receipts}
    if control_problems:
        result["invalid"] = control_problems
        return 2, result
    result["problems"] = problems
    return (1 if problems else 0), result


# ── P2 ───────────────────────────────────────────────────────────────────────


def extract_file(path: pathlib.Path, start: float, end: float) -> "dict | None":
    """Prefix bytes before the first line at or after START, then the
    contiguous lines up to END with their relative times (a line without a
    timestamp keeps the previous line's), or None when nothing falls inside."""
    with open(path, "rb") as fh:
        data = fh.read()
    offset = 0
    first = None
    lines = []
    last_at = None
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break                       # a line still being written
        at = _line_instant(raw)
        if first is None:
            if at is not None and at >= start:
                first = offset
            else:
                offset += len(raw)
                continue
        effective = at if at is not None else last_at
        if effective is not None and effective > end:
            break
        effective = max(effective if effective is not None else start,
                        last_at if last_at is not None else start)
        lines.append((effective - start, raw))
        last_at = effective
        offset += len(raw)
    if first is None or not lines:
        return None
    return {"prefixBytes": first, "prefix": data[:first], "lines": lines}


def p2_extract(start: float, end: float, out: pathlib.Path, roots) -> dict:
    out = out.resolve()
    for root in roots:
        if root.resolve() == out or root.resolve() in out.parents:
            raise SystemExit("the slice may not be written inside a source root")
    out.mkdir(parents=True, exist_ok=False)
    (out / "prefix").mkdir()
    (out / "window").mkdir()
    files = []
    for root in roots:
        walk = _walk_root(root)
        for path in sorted(walk.glob("**/*.jsonl")):
            try:
                if not path.is_file() or path.stat().st_mtime < start - 3600:
                    continue
            except OSError:
                continue
            found = extract_file(path, start, end)
            if found is None:
                continue
            n = len(files)
            (out / "prefix" / f"{n}.jsonl").write_bytes(found["prefix"])
            blob = b"".join(raw for _dt, raw in found["lines"])
            (out / "window" / f"{n}.bin").write_bytes(blob)
            index, offset = [], 0
            for rel, raw in found["lines"]:
                index.append({"dt": round(rel, 3), "offset": offset,
                              "length": len(raw)})
                offset += len(raw)
            (out / "window" / f"{n}.json").write_text(json.dumps(index) + "\n")
            files.append({"id": n, "source": str(path),
                          "relative": str(path.resolve().relative_to(
                              walk.resolve())),
                          "prefixBytes": found["prefixBytes"],
                          "createdInWindow": found["prefixBytes"] == 0,
                          "lines": len(index), "bytes": len(blob)})
    manifest = {"window": {"start": _now_iso(start), "end": _now_iso(end),
                           "seconds": end - start},
                "roots": [str(r) for r in roots], "files": files,
                "bytes": sum(f["bytes"] for f in files),
                "lines": sum(f["lines"] for f in files)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _slice_target(root: pathlib.Path, entry: dict) -> pathlib.Path:
    """Where one extracted file lives in the clone's scratch Codex root (each
    under its own id, so two roots' same-named rollouts never collide)."""
    return (pathlib.Path(root) / "scratch" / "codex" / "sessions" / "p2"
            / str(entry["id"]) / entry["relative"])


def p2_place(slice_dir: pathlib.Path, root: pathlib.Path) -> dict:
    """Each file truncated at the window start, under the clone's scratch root
    (a file created inside the window is created by the replay instead)."""
    manifest = json.loads((slice_dir / "manifest.json").read_text())
    placed = 0
    for entry in manifest["files"]:
        if entry["createdInWindow"]:
            continue
        target = _inside(root, _slice_target(root, entry))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((slice_dir / "prefix" / f"{entry['id']}.jsonl").read_bytes())
        placed += 1
    return {"placed": placed, "files": len(manifest["files"])}


def replay_schedule(slice_dir: pathlib.Path) -> list:
    """[(dt, file id, bytes)] in replay order."""
    manifest = json.loads((slice_dir / "manifest.json").read_text())
    events = []
    for entry in manifest["files"]:
        blob = (slice_dir / "window" / f"{entry['id']}.bin").read_bytes()
        for row in json.loads((slice_dir / "window" / f"{entry['id']}.json").read_text()):
            events.append((row["dt"], entry["id"],
                           blob[row["offset"]:row["offset"] + row["length"]]))
    events.sort(key=lambda e: (e[0], e[1]))
    return events


def p2_replay(slice_dir: pathlib.Path, root: pathlib.Path, seconds: float,
              time_scale: float = 1.0) -> dict:
    manifest = json.loads((slice_dir / "manifest.json").read_text())
    targets = {e["id"]: _inside(root, _slice_target(root, e))
               for e in manifest["files"]}
    started = time.monotonic()
    written = lines = 0
    late = 0.0
    for at, file_id, raw in replay_schedule(slice_dir):
        due = started + at / time_scale
        if due - started > seconds:
            break
        wait = due - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        late = max(late, time.monotonic() - due)
        target = targets[file_id]
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "ab") as fh:
            fh.write(raw)
        written += len(raw)
        lines += 1
    return {"bytes": written, "lines": lines, "maxLateS": round(late, 3),
            "timeScale": time_scale, "files": len(targets)}


# ── CLI ──────────────────────────────────────────────────────────────────────


def _emit(result, code) -> int:
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    print({0: "PASS", 1: "FAIL", 2: "INVALID"}[code])
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("p1")
    p.add_argument("--tree", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--conversation", required=True)
    p.add_argument("--late-anchor", action="store_true")
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--role", choices=("candidate", "control"), default="candidate")
    p.add_argument("--dylib")
    p.add_argument("--real-codex-home")
    p.add_argument("--out", required=True)
    p = sub.add_parser("p1-verdict")
    p.add_argument("receipts", nargs="+")
    p.add_argument("--reps", type=int, default=5)
    p = sub.add_parser("p2-extract")
    p.add_argument("--window", nargs=2, required=True, metavar=("START", "END"))
    p.add_argument("--out", required=True)
    p.add_argument("--codex-home")
    p = sub.add_parser("p2-place")
    p.add_argument("--slice", required=True)
    p.add_argument("--root", required=True)
    p = sub.add_parser("p2-replay")
    p.add_argument("--slice", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--time-scale", type=float, default=1.0)
    p = sub.add_parser("_pass")
    p.add_argument("--tree", required=True)
    p.add_argument("--step", required=True,
                   choices=("forced", "conversations", "cache", "export"))
    p.add_argument("--case", required=True)
    p.add_argument("--conversation", default="")
    p.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    if args.cmd in ("_pass", "p1"):
        sys.path.insert(0, str(HERE))
        import frozen_roots
        frozen_roots.require_namespace("projection_replay.py")
    if args.cmd == "_pass":
        return run_pass(args)
    if args.cmd == "p1":
        receipt = P1(args).run()
        pathlib.Path(args.out).write_text(
            json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n")
        problems = _receipt_problems(receipt, args.reps)
        print(json.dumps({"out": args.out, "problems": problems,
                          "summary": receipt["summary"]}, default=str))
        return 2 if problems else 0
    if args.cmd == "p1-verdict":
        receipts = [json.loads(pathlib.Path(p).read_text()) for p in args.receipts]
        sys.path.insert(0, str(HERE))
        import frozen_roots
        inputs = [{"problems": frozen_roots.check_inputs(pathlib.Path(p).parent),
                   "label": frozen_roots.evidence_label(pathlib.Path(p).parent)}
                  for p in args.receipts]
        code, result = p1_verdict(receipts, reps=args.reps, inputs=inputs)
        return _emit(result, code)
    if args.cmd == "p2-extract":
        start, end = (_parse_instant(v) for v in args.window)
        if end <= start:
            parser.error("the window must end after it starts")
        manifest = p2_extract(start, end, pathlib.Path(args.out),
                              _codex_roots(args.codex_home))
        print(json.dumps({k: manifest[k] for k in ("window", "bytes", "lines")}
                         | {"files": len(manifest["files"])}))
        return 0
    if args.cmd == "p2-place":
        print(json.dumps(p2_place(pathlib.Path(args.slice), pathlib.Path(args.root))))
        return 0
    print(json.dumps(p2_replay(pathlib.Path(args.slice), pathlib.Path(args.root),
                               args.seconds, args.time_scale)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
