"""#901 spec §6.3 "Frozen input set" / "Frozen namespace" (Q16, `dc13` L1-L2,
901-SR-037; the precision corrections Q17, `dc14` N1-N3, and Amendment 11):
capture, seal, verify and qualify one frozen copy of the transcript inputs per
measurement session, bound to every store copy it serves, and judge a run
family's namespace receipts.

usage:
  frozen_roots.py capture --store SRC_ROOT [--store SRC_ROOT ...] --out FREEZE
                          [--home H] [--codex-home ROOTS]
                          [--claude-config-dir DIRS] [--extra PATH ...]
                          [--probe PATH ...] [--retry-deadline-s 120]
  frozen_roots.py verify  --freeze FREEZE [--full] [--out JSON]
  frozen_roots.py qualify --store SRC_ROOT --freeze FREEZE [--out JSON]
  frozen_roots.py family  --receipts DIR --freeze FREEZE [--expect-pid PID ...]
                          [--out JSON]

capture  One freeze per measurement session, bound to EVERY store copy it
         serves (one --store each: the candidate's and the `56e66f07a`
         baseline's copies differ, because each tree needs its own schema), so
         matched runs on different copies compare on one freeze. Refuses
         unless each SRC_ROOT/data (a backup-API copy of the store) is closed
         (no WAL or journal content) and older than the capture, and a copy
         named twice. Reads the live roots READ-ONLY, every store read-only
         (`immutable=1`), and writes only FREEZE. The manifest's `stores`
         lists every bound copy's identity, in --store order. The input set
         is what the tested tree reads
         outside its data directory: each configured Codex home (`$CODEX_HOME`,
         else H/.codex) with its walked rollouts (`sessions/**/*.jsonl`, or the
         home itself when it has no `sessions/`), its `auth.json`,
         `config.toml` and `hooks.json` (whole) and `state_5.sqlite` (a
         DERIVED input: its main file and -wal read raw as one stability unit,
         then recovered, checkpointed and switched to rollback mode on a
         scratch copy only, see `capture_sqlite_pair`); each Claude data directory's `projects/**/*.jsonl`
         (`$CLAUDE_CONFIG_DIR`, else H/.config/claude and H/.claude); H/.claude.json;
         every `--extra` file (the namespace self-test's observed reads); and
         every path any bound copy retains under a root. Absent inputs are
         RECORDED absences, never silent. Append-only JSONL is copied as a
         prefix ending at a complete record boundary at or beyond every cursor
         and scan target (`last_byte_offset`, `size_bytes`) ANY bound copy
         retains (the largest over the union of the copies), with the
         source device, inode, admitted length, timestamps and prefix hash
         checked before and after: suffix growth during the copy is admitted,
         anything else is retried until the retry deadline and then refused.
         Every other input is copied WHOLE by a stable read (identity, length,
         timestamps and whole-file hash unchanged across the copy; an in-place
         rewrite is retried, then refused; never truncated). Symlinks are
         covered or refused (Amendment 10): a file link whose target is inside
         a root is covered by that root's file; an absolute, single-hop link
         to a regular file outside every root is covered by capturing the
         target under its own single-file root (`origin: link-target`, its
         live path denied like a root); inside the freeze a covered file link
         is a relative link to the frozen copy, `readlink` returns the stored
         target and everything through it reaches the frozen copy with the
         target's source identity. A directory link the walk does not
         traverse is covered as non-traversed (`class: dir-link`): presented
         as a symlink, nothing under it captured, its live target denied
         (`deniedTargets`, `X` records), so a traversal is killed. The
         target directory's own lstat is recorded (`deniedTargets[].metadata`,
         never its contents; Amendment 13 O1), so the namespace answers a
         stat that FOLLOWS the link (os.walk's DirEntry.is_dir) or a stat of
         the target itself without a kernel call on the live target; any
         other access of the target or below is a refused `traversal`. A relative
         or chained out-of-root link, a link to a directory or a non-regular
         target used as a file, a symlinked root or a path with a tab or
         newline is refused. Probe-only entries (Amendment 13 O2): after the
         copy, every working directory recorded in the FROZEN transcripts
         (Codex session_meta / turn_context `payload.cwd`, Claude `cwd`,
         streamed line by line within each captured prefix) that lies under
         a directory root gets each component below the root and each
         existing directory's `.git` recorded from the live tree - present
         (`class: probe`: its lstat, and for a link its stored target and
         `followed` stat) or a recorded absence (`probe-absent`) - metadata
         only, nothing copied; every `--probe PATH` too (its components, no
         `.git`). The namespace presents lstat/stat/access from them and
         refuses an open or listing (`unmapped-open`). The product's git-root
         walk-up from a Codex worktree cwd under ~/.codex reads them. A cwd
         AT or BELOW a non-traversed link's denied target outside every root
         (Amendment 14 P1: the real transcripts record a project's
         `.claude-memory` as their cwd) is probed the same way inside that
         target - its `.git`, each component below it and each existing
         directory's `.git` (`root: null`, `deniedTarget`) - and so is a
         `--probe PATH` at or below one; the target itself keeps its O1
         presentation and any other access inside it stays a `traversal`.
         Writes manifest.json, rootmap.tsv
         (the namespace library's manifest), frozen.sb (the sandbox profile:
         every live root and metadata file denied, the freeze write-denied, a
         violating process killed) and seal.json, then makes the freeze
         read-only. Exit 0 CAPTURED, 2 REFUSED.
verify   Recomputes the seal: the control files' hashes and the metadata digest
         of every frozen path (kind, size, mode, mtime, inode) - and, with
         --full, every file's content hash. Runners call it before and after
         each run. Exit 0 VERIFIED, 2 INVALID.
qualify  Qualifies ONE bound copy against the shared freeze; run it once per
         copy. Refuses a copy the freeze is not bound to (its root is not in
         the manifest's `stores`) or whose databases changed since the
         capture, and the (store copy, freeze) pair when a retained cursor or
         scan target is beyond its frozen file, a retained file was replaced
         (stored inode != captured inode), a committed prefix digest
         (conversation_source_files) differs from the frozen file's first
         bytes, or a file shrank or is missing - except a
         genuine pre-existing residual that SRC_ROOT/prep-receipt.json
         (`catchup.py prep`) confirmed absent. The refusal is per copy: a
         later drain of one copy refuses that copy only. Writes a
         qualification receipt naming the copy (`store`) and the freeze
         (default SRC_ROOT/frozen-qualification.json). Exit 0 QUALIFIED, 2
         REFUSED.
family   Judges the rootmap receipts of one run family (rootmap.c writes them):
         every activation loaded THIS freeze's manifest and was enforced by
         the sandbox, no process recorded an unmapped, denied or write-denied
         access, every exec or spawn of a non-system image activated, and
         every launched or expected pid has an activation. A process started
         through a protected system program (Q17) is admitted without
         activation only with its parent's launch record (program, arguments,
         parent and child pid) and its recorded termination status, and is
         listed; no family member may end on a signal unless the harness
         recorded that teardown kill (teardown.jsonl, written by `reap`) or a
         recorded family member sent that exact signal to that child (its
         kill/killpg record; listed under `familyKills`, such as the
         product's `npm prefix -g` timeout); a signal no member sent stays
         fatal. A top-level family process (started by the runner through
         the launch contract; its parent is the runner's shell, which the
         namespace does not load) is judged from the runner's record of how
         it ended (toplevel.jsonl, Amendment 13 O3) by the same rules, and
         every launched pid must have such a record. A `traversal` or
         `unmapped-open` event is INVALID like an unmapped read. Exit 0
         VALID, 2 INVALID.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import stat as stat_mod
import sys
import time

#: frozen-roots/2 (Amendment 11): `stores`, the list of every bound store
#: copy's identity, replaces /1's single `store`.
SCHEMA = "frozen-roots/2"
SEAL_SCHEMA = "frozen-seal/1"
QUALIFY_SCHEMA = "frozen-qualification/1"
FAMILY_SCHEMA = "frozen-family/1"
RECEIPT_SCHEMA = "rootmap/1"
DATABASES = ("cache.db", "conversations.db", "stats.db")
#: (store, table) whose rows name retained transcript paths, with their
#: cursor and scan-target columns and the identity columns each carries.
RETAINED = (
    ("cache.db", "session_files", ("last_byte_offset", "size_bytes"), ()),
    ("cache.db", "codex_session_files", ("last_byte_offset", "size_bytes"),
     ("device_id", "inode")),
    ("conversations.db", "conversation_source_files",
     ("last_byte_offset", "size_bytes"),
     ("device_id", "inode", "committed_prefix_sha256")),
    ("conversations.db", "codex_conversation_source_files",
     ("last_byte_offset", "size_bytes"), ("device_id", "inode")),
)
#: Protected system programs (spec §6.3 "Frozen namespace", Q17): macOS
#: keeps library injection out of an executable under these prefixes, or a
#: "#!" script whose interpreter is one, so it cannot carry the namespace. Such
#: a process (and any image it executes) is admitted without activation only
#: with its parent's launch record and recorded termination status.
SYSTEM_PREFIXES = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/libexec/",
                   "/System/")
#: Files of a receipts directory that are not per-process receipts: the
#: launcher's launch log and the harness's own recorded teardown kills.
LAUNCHES = "launches.jsonl"
TEARDOWN = "teardown.jsonl"
#: How each top-level family process ended (Amendment 13 O3): its parent is
#: the runner's shell, which the namespace does not load, so the runner
#: records it (`_inputs.sh` wa_wait; hook_runner.py for its hooks).
TOPLEVEL = "toplevel.jsonl"
TOPLEVEL_SCHEMA = "toplevel/1"
CONTROL_FILES = ("manifest.json", "rootmap.tsv", "frozen.sb")
#: Root-local inputs of each Codex home the tested family reads besides its
#: rollouts (observed by running the family through the namespace: attribution
#: reads auth.json, the cost path config.toml, the doctor hooks.json, the
#: dashboard's title enrichment state_5.sqlite). A missing one is a recorded
#: absence; anything else the family reads is named with --extra.
CODEX_METADATA = ("auth.json", "config.toml", "hooks.json", "state_5.sqlite")
#: SQLite databases among those inputs (spec §6.3, Q17, `dc14` N1). Codex
#: writes them through a WAL, so a title can be committed only in the -wal; the
#: freeze holds a DERIVED single rollback-mode file (no sidecar, so a read-only
#: reader of the write-denied freeze needs no shared memory) recovered from a
#: stable raw capture of the main file and -wal together.
SQLITE_METADATA = ("state_5.sqlite",)
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
#: Scratch directory inside the freeze being captured where SQLite recovers
#: the copied pair; removed before the seal (never part of the freeze).
DERIVE_DIR = ".sqlite-derive"
CHUNK = 1 << 20


class Refused(Exception):
    """The capture or qualification cannot certify this input."""


def _utc(at: "float | None" = None) -> str:
    when = dt.datetime.fromtimestamp(time.time() if at is None else at,
                                     dt.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha_file(path, length=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        left = length
        while left is None or left > 0:
            chunk = fh.read(CHUNK if left is None else min(CHUNK, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm(path) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


# ── the input set ─────────────────────────────────────────────────────────

class Root:
    """One logical root: kind codex-home | claude-projects | file; `walk` is
    the directory whose **/*.jsonl the tree walks (a plain class: the tests
    load this module without registering it, which a dataclass needs).
    `origin` "link-target" marks a single-file root the capture added for the
    target of a covered link outside every root, `links` its links."""

    def __init__(self, kind: str, logical: str, walk, metadata=(),
                 present: bool = True, index: int = 0, origin=None) -> None:
        self.kind, self.logical, self.walk = kind, logical, walk
        self.metadata, self.present, self.index = tuple(metadata), present, index
        self.origin, self.links = origin, []

    @property
    def is_file(self) -> bool:
        return self.kind == "file"


def codex_homes(raw: "str | None", home: str) -> "list[str]":
    """The tree's `_codex_home_roots()`: every comma-split $CODEX_HOME entry,
    expanded against H and made absolute, else [H/.codex]."""
    if not raw or not raw.strip():
        return [_norm(os.path.join(home, ".codex"))]
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("~"):
            part = home + part[1:]
        out.append(_norm(part))
    return out or [_norm(os.path.join(home, ".codex"))]


def claude_dirs(raw: "str | None", home: str) -> "tuple[list[str], list[str]]":
    """(read, absent): the tree's `_get_claude_data_dirs()` candidates; the
    ones it would read, and the default candidates it would probe and find
    absent (recorded as absences so a later appearance is not read live)."""
    if raw and raw.strip():
        dirs = [_norm(p.strip()) for p in raw.split(",") if p.strip()]
        found = [d for d in dirs
                 if os.path.isdir(d) and os.path.isdir(os.path.join(d, "projects"))]
        if found:
            return found, []
    defaults = [_norm(os.path.join(home, ".config", "claude")),
                _norm(os.path.join(home, ".claude"))]
    found = [d for d in defaults
             if os.path.isdir(d) and os.path.isdir(os.path.join(d, "projects"))]
    return found, [d for d in defaults if d not in found]


def input_roots(home: str, codex_home: "str | None",
                claude_config_dir: "str | None", extras=()) -> "list[Root]":
    roots: "list[Root]" = []
    for h in codex_homes(codex_home, home):
        sessions = os.path.join(h, "sessions")
        walk = sessions if os.path.isdir(sessions) else h
        roots.append(Root("codex-home", h, walk,
                          tuple(os.path.join(h, name) for name in CODEX_METADATA),
                          present=os.path.isdir(h)))
    read, missing = claude_dirs(claude_config_dir, home)
    for d in read:
        projects = os.path.join(d, "projects")
        roots.append(Root("claude-projects", projects, projects))
    for d in missing:
        roots.append(Root("claude-projects", os.path.join(d, "projects"), None,
                          present=False))
    claude_json = _norm(os.path.join(home, ".claude.json"))
    roots.append(Root("file", claude_json, None,
                      present=os.path.lexists(claude_json)))
    for extra in extras:
        path = _norm(extra)
        if not any(_under(path, r.logical) for r in roots if not r.is_file):
            roots.append(Root("file", path, None, present=os.path.lexists(path)))
    for i, root in enumerate(roots):
        root.index = i
    return roots


# ── the store copy (read-only) ────────────────────────────────────────────

def _immutable(path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def store_identity(store) -> dict:
    """{root, data, closed, problems, databases{size, mtimeNs, userVersion},
    newestMtime}. Closed means no WAL or rollback-journal content."""
    data = pathlib.Path(store) / "data"
    out = {"root": os.path.realpath(store), "data": str(data), "databases": {},
           "problems": []}
    newest = 0.0
    if not data.is_dir():
        out["problems"].append(f"no data directory in {store}")
    for name in DATABASES:
        db = data / name
        entry = {"present": db.exists()}
        if db.exists():
            st = db.stat()
            newest = max(newest, st.st_mtime)
            entry.update(size=st.st_size, mtimeNs=st.st_mtime_ns)
            for side in ("-wal", "-journal"):
                extra = pathlib.Path(str(db) + side)
                if extra.exists() and extra.stat().st_size:
                    out["problems"].append(f"{name}{side} is not empty: the "
                                           "copy is not closed")
                if extra.exists():
                    newest = max(newest, extra.stat().st_mtime)
            try:
                conn = _immutable(db)
                try:
                    entry["userVersion"] = conn.execute(
                        "PRAGMA user_version").fetchone()[0]
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                out["problems"].append(f"{name} unreadable: {exc}")
        out["databases"][name] = entry
    out["newestMtime"] = newest
    out["closed"] = not out["problems"]
    return out


def retained_rows(store) -> "list[dict]":
    """Every retained transcript row: {store, table, path, offsets{col: n},
    identity{col: n}}. A missing table or column is simply absent."""
    data = pathlib.Path(store) / "data"
    rows = []
    for name, table, offsets, identity in RETAINED:
        db = data / name
        if not db.exists():
            continue
        conn = _immutable(db)
        try:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "path" not in have:
                continue
            cols = [c for c in offsets + identity if c in have]
            select = ", ".join(["path"] + cols)
            for row in conn.execute(f"SELECT {select} FROM {table}"):
                values = dict(zip(cols, row[1:]))
                rows.append({
                    "store": name, "table": table, "path": row[0],
                    "offsets": {c: int(values[c] or 0) for c in offsets
                                if c in values},
                    "identity": {c: values[c] for c in identity if c in values
                                 and values[c] is not None}})
        except sqlite3.Error as exc:
            raise Refused(f"{name}.{table} unreadable: {exc}") from exc
        finally:
            conn.close()
    return rows


# ── copying ───────────────────────────────────────────────────────────────

class Clock:
    """Injectable monotonic clock (tests advance it without sleeping)."""

    def __init__(self, fn=time.monotonic) -> None:
        self.fn = fn

    def __call__(self) -> float:
        return self.fn()


def _times(st) -> dict:
    return {"atimeNs": st.st_atime_ns, "mtimeNs": st.st_mtime_ns,
            "ctimeNs": st.st_ctime_ns,
            "birthtimeNs": int(getattr(st, "st_birthtime", 0) * 1e9)
            if not hasattr(st, "st_birthtime_ns") else st.st_birthtime_ns}


def _identity(st) -> dict:
    return {"dev": st.st_dev, "ino": st.st_ino, "mode": st.st_mode,
            "nlink": st.st_nlink, "uid": st.st_uid, "gid": st.st_gid,
            **_times(st)}


def _file_type(mode: int) -> str:
    return ("dir" if stat_mod.S_ISDIR(mode) else "file" if stat_mod.S_ISREG(mode)
            else "symlink" if stat_mod.S_ISLNK(mode) else "other")


def _meta(st) -> dict:
    """Metadata only (Amendment 13): what the namespace presents for a path it
    never opens - a non-traversed directory link's target, or a probe entry."""
    return {"type": _file_type(st.st_mode), **_identity(st), "size": st.st_size}


def copy_prefix(src: str, dst: str, required: int, *, deadline: float,
                clock=time.monotonic, on_copy=None) -> dict:
    """Copy an append-only JSONL file's prefix ending at its last complete
    record boundary, which must be >= `required`. Retried until `deadline`
    (a `clock()` value); raises Refused after it. `on_copy(src, attempt)` is a
    test seam called after each copy pass, before the after-checks."""
    attempt = 0
    last = "no attempt"
    while True:
        if attempt and clock() > deadline:
            raise Refused(f"{src}: {last}; the retry deadline expired")
        attempt += 1
        st0 = os.lstat(src)
        if not stat_mod.S_ISREG(st0.st_mode):
            raise Refused(f"{src}: not a regular file")
        h = hashlib.sha256()
        boundary = 0
        pending = bytearray()
        tmp = dst + ".part"
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            left = st0.st_size
            while left > 0:
                chunk = fin.read(min(CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
                fout.write(chunk)
                cut = chunk.rfind(b"\n")
                if cut < 0:
                    pending += chunk
                    continue
                h.update(pending)
                h.update(chunk[:cut + 1])
                boundary += len(pending) + cut + 1
                pending = bytearray(chunk[cut + 1:])
            fout.truncate(boundary)
        digest = h.hexdigest()
        if on_copy is not None:
            on_copy(src, attempt)
        if boundary < required:
            last = (f"the last complete record ends at {boundary}, before the "
                    f"retained cursor/scan target {required}")
            os.unlink(tmp)
            continue
        st1 = os.lstat(src)
        if (st1.st_dev, st1.st_ino) != (st0.st_dev, st0.st_ino):
            last = "the source was replaced during the copy"
            os.unlink(tmp)
            continue
        if st1.st_size < boundary:
            last = "the source shrank during the copy"
            os.unlink(tmp)
            continue
        changed = (st1.st_size, st1.st_mtime_ns, st1.st_ctime_ns) != (
            st0.st_size, st0.st_mtime_ns, st0.st_ctime_ns)
        if changed and _sha_file(src, boundary) != digest:
            last = "the copied prefix changed during the copy (in-place rewrite)"
            os.unlink(tmp)
            continue
        os.replace(tmp, dst)
        return {**_identity(st0), "sourceSize": st0.st_size,
                "admittedLength": boundary, "sha256": digest,
                "grewDuringCopy": changed, "attempts": attempt,
                "requiredOffset": required}


def copy_whole(src: str, dst: str, *, deadline: float, clock=time.monotonic,
               on_copy=None) -> dict:
    """Stable whole-file read of a non-append input (multiline JSON included):
    identity, length, timestamps and whole-file hash unchanged across the
    copy, else retried until `deadline` and then refused. Never truncated."""
    attempt = 0
    last = "no attempt"
    while True:
        if attempt and clock() > deadline:
            raise Refused(f"{src}: {last}; the retry deadline expired")
        attempt += 1
        st0 = os.lstat(src)
        if not stat_mod.S_ISREG(st0.st_mode):
            raise Refused(f"{src}: not a regular file")
        with open(src, "rb") as fh:
            data = fh.read()
        if on_copy is not None:
            on_copy(src, attempt)
        st1 = os.lstat(src)
        with open(src, "rb") as fh:
            again = fh.read()
        key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                         s.st_ctime_ns)
        if key(st0) != key(st1) or len(data) != st0.st_size \
                or _sha_bytes(data) != _sha_bytes(again):
            last = "the file changed during its copy (in-place rewrite)"
            continue
        with open(dst, "wb") as fh:
            fh.write(data)
        return {**_identity(st0), "sourceSize": st0.st_size,
                "admittedLength": len(data), "sha256": _sha_bytes(data),
                "attempts": attempt}


def _lstat_or_none(path: str):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _copy_raw(src: str, dst) -> "tuple[int, str]":
    """Copy `src` by plain reads (never through SQLite): (length, sha256 of
    the bytes copied)."""
    h = hashlib.sha256()
    length = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for chunk in iter(lambda: fin.read(CHUNK), b""):
            fout.write(chunk)
            h.update(chunk)
            length += len(chunk)
    return length, h.hexdigest()


def _pair_change(name: str, before, after) -> "str | None":
    """Why one file of the pair differs across the capture interval: its
    presence, device and inode, length, mtime or ctime."""
    if (before is None) != (after is None):
        return f"the {name} {'appeared' if before is None else 'disappeared'}"
    if before is None:
        return None
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        return f"the {name} was replaced"
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        return f"the {name} changed (length, mtime or ctime)"
    return None


def _journal(main: str, journal: str) -> "int | None":
    """The rollback journal's length (None when absent); an unexpected
    nonempty one refuses the capture (Codex keeps the database in WAL mode, so
    a hot journal means the pair is not a WAL-mode database at rest)."""
    st = _lstat_or_none(journal)
    if st is None:
        return None
    if not stat_mod.S_ISREG(st.st_mode) or st.st_size:
        raise Refused(f"{main}: an unexpected nonempty rollback journal "
                      f"({journal}, {st.st_size} bytes) refuses the capture")
    return 0


def capture_sqlite_pair(main: str, scratch, *, deadline: float,
                        clock=time.monotonic, on_pair=None) -> dict:
    """Spec §6.3 "Frozen input set" (Q17, `dc14` N1): capture a WAL-mode
    SQLite input's main file and -wal as ONE stability unit, by raw reads only
    on the live paths, into SCRATCH (an external scratch directory). Presence,
    device and inode, length, mtime and ctime of both files must be unchanged
    across the whole pre-hash -> copy -> post-hash interval, and each file's
    pre-hash, copied-byte hash and post-hash must agree; WAL absence must be
    stable. Any change retries the WHOLE pair until `deadline` (a `clock()`
    value), then refuses. A constant WAL length proves nothing (SQLite reuses
    its high-water allocation with new salts), so the hashes decide. The -shm
    is neither copied nor used; a nonempty -journal refuses. `on_pair(main,
    stage, attempt)` is a test seam called after the main file's copy
    (stage "main") and after both copies ("copied"), before the after-checks.
    Returns {source, identity, recovery, derived}: the source provenance, the
    main file's captured identity, and the recovery of the scratch copy
    (`recover_sqlite`) whose result is the single derived file `derived`."""
    wal, shm, journal = main + "-wal", main + "-shm", main + "-journal"
    scratch = pathlib.Path(scratch)
    s_main = scratch / os.path.basename(main)
    s_wal = scratch / (os.path.basename(main) + "-wal")
    attempt, retries = 0, []
    while True:
        if attempt and clock() > deadline:
            raise Refused(f"{main}: {retries[-1]}; the retry deadline expired "
                          "(the main file and its WAL never held still together)")
        attempt += 1
        for leftover in scratch.iterdir():
            leftover.unlink()
        j0 = _journal(main, journal)
        started_wall, started = time.time(), time.monotonic()
        m0, w0 = _lstat_or_none(main), _lstat_or_none(wal)
        if m0 is None:
            retries.append("the main file is missing")
            continue
        for label, st in (("main file", m0), ("WAL", w0)):
            if st is not None and not stat_mod.S_ISREG(st.st_mode):
                raise Refused(f"{main}: the {label} is not a regular file")
        try:
            pre_m = _sha_file(main)
            pre_w = _sha_file(wal) if w0 is not None else None
            len_m, copied_m = _copy_raw(main, s_main)
            if on_pair is not None:
                on_pair(main, "main", attempt)
            len_w = copied_w = None
            if w0 is not None:
                len_w, copied_w = _copy_raw(wal, s_wal)
            if on_pair is not None:
                on_pair(main, "copied", attempt)
            post_m = _sha_file(main)
            post_w = _sha_file(wal) if w0 is not None else None
        except FileNotFoundError as exc:
            retries.append(f"a file of the pair disappeared during its copy "
                           f"({exc.filename})")
            continue
        m1, w1 = _lstat_or_none(main), _lstat_or_none(wal)
        j1 = _journal(main, journal)
        problem = _pair_change("main file", m0, m1) or _pair_change("WAL", w0, w1)
        if problem is None and not (pre_m == copied_m == post_m
                                    and len_m == m0.st_size):
            problem = "the main file's bytes changed during the copy"
        if problem is None and w0 is not None and not (
                pre_w == copied_w == post_w and len_w == w0.st_size):
            problem = "the WAL's bytes changed during the copy"
        if problem is None and j0 != j1:
            problem = "the rollback journal appeared or disappeared"
        if problem is not None:
            retries.append(problem)
            continue
        ended_wall, ended = time.time(), time.monotonic()
        break
    source = {
        "main": {"logical": main, **_identity(m0), "size": m0.st_size,
                 "sha256": pre_m},
        "wal": ({"logical": wal, "present": True, **_identity(w0),
                 "size": w0.st_size, "sha256": pre_w} if w0 is not None
                else {"logical": wal, "present": False, "stableAbsence": True}),
        "shm": {"logical": shm, "present": os.path.lexists(shm),
                "copied": False, "used": False},
        "journal": ({"logical": journal, "present": False} if j1 is None
                    else {"logical": journal, "present": True, "size": j1}),
        "captureInterval": {"start": _utc(started_wall), "end": _utc(ended_wall),
                            "seconds": round(ended - started, 6)},
        "attempts": attempt, "retries": retries[-20:],
    }
    return {"source": source, "identity": _identity(m0), "sourceSize": m0.st_size,
            "recovery": recover_sqlite(s_main, logical=main), "derived": s_main}


def recover_sqlite(path, *, logical: str) -> dict:
    """On an external scratch copy ONLY (never a live path): let SQLite recover
    the copied pair's committed state, complete `wal_checkpoint(TRUNCATE)`,
    switch to `journal_mode=DELETE`, pass `integrity_check` and close, leaving
    one rollback-mode file and no sidecar. Any failure refuses the capture.
    Returns the recovery runtime and results."""
    path = pathlib.Path(path)
    try:
        conn = sqlite3.connect(str(path), isolation_level=None)
        try:
            source_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            checkpoint = list(conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
            integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise Refused(f"{logical}: SQLite recovery of the scratch copy failed: "
                      f"{exc}") from exc
    result = {"sqlite": sqlite3.sqlite_version, "python": sys.version.split()[0],
              "sourceJournalMode": str(source_mode).lower(),
              "walCheckpoint": checkpoint, "journalMode": mode.lower(),
              "integrityCheck": integrity[0] if integrity == ["ok"] else integrity,
              "pageSize": page_size, "pageCount": page_count}
    if checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
        raise Refused(f"{logical}: recovery's wal_checkpoint(TRUNCATE) did not "
                      f"complete on the scratch copy ({checkpoint})")
    if result["journalMode"] != "delete":
        raise Refused(f"{logical}: recovery could not leave WAL mode ({mode})")
    if integrity != ["ok"]:
        raise Refused(f"{logical}: the recovered scratch copy failed its "
                      f"integrity check ({integrity[:5]})")
    leftovers = sorted(p.name for p in path.parent.iterdir() if p.name != path.name)
    if leftovers:
        raise Refused(f"{logical}: recovery left sidecars on the scratch copy "
                      f"({leftovers})")
    with open(path, "rb") as fh:
        header = fh.read(100)
    if header[18:20] != b"\x01\x01":
        raise Refused(f"{logical}: the recovered copy is not a rollback-mode "
                      "database")
    result["sidecarsAfterClose"] = []
    return result


# ── capture ───────────────────────────────────────────────────────────────

def _check_path(path: str) -> None:
    if "\t" in path or "\n" in path:
        raise Refused(f"a path with a tab or newline cannot be mapped: {path!r}")


def _outside_link_target(path: str, raw: str) -> str:
    """The logical target of a file link whose target lies outside every root
    (Amendment 10; the real ~/.codex/sessions holds such links to rollouts
    under other repositories' .codex/sessions). Covered only when absolute,
    single-hop - the target is no link and no linked directory lies on its
    path, so its spelling is the canonical path the tree stores (Codex's
    `Path.resolve()`) - and a regular file; anything else is refused."""
    if not os.path.isabs(raw):
        raise Refused(f"a relative symlink to a file outside every root is refused: "
                      f"{path} -> {raw}")
    target = os.path.normpath(raw)
    try:
        st = os.lstat(target)
    except OSError as exc:
        raise Refused(f"a symlink to a target that is not a regular file is refused "
                      f"({exc.strerror}): {path} -> {raw}") from exc
    if stat_mod.S_ISLNK(st.st_mode):
        raise Refused(f"a chain of links is refused: {path} -> {raw} -> "
                      f"{os.readlink(target)}")
    if stat_mod.S_ISDIR(st.st_mode):
        raise Refused(f"a link to a directory used as a file is refused: "
                      f"{path} -> {raw}")
    if not stat_mod.S_ISREG(st.st_mode):
        raise Refused(f"a symlink to a target that is not a regular file is refused: "
                      f"{path} -> {raw}")
    canonical = os.path.realpath(target)
    if canonical != target:
        raise Refused(f"a chain of links (a linked directory on the target's path) is "
                      f"refused: {path} -> {raw}, canonically {canonical}")
    _check_path(target)
    return target


def _frozen(freeze: pathlib.Path, root: Root, logical: str) -> pathlib.Path:
    base = freeze / "roots" / str(root.index)
    if root.is_file:
        return base
    rel = os.path.relpath(logical, root.logical)
    return base if rel == "." else base / rel


def _root_of(roots, path: str) -> "Root | None":
    best = None
    for root in roots:
        if (root.is_file and path == root.logical) or (
                not root.is_file and _under(path, root.logical)):
            if best is None or len(root.logical) > len(best.logical):
                best = root
    return best


def bind_stores(stores, started_wall: float) -> "list[dict]":
    """The identities of every store copy one freeze is bound to (Amendment
    11), in the given order. Refuses an empty list, a copy named twice, and a
    copy that is open or not older than the capture, naming the copy."""
    copies = [stores] if isinstance(stores, (str, os.PathLike)) else list(stores)
    if not copies:
        raise Refused("no store copy to bind the freeze to (--store)")
    bound: "list[dict]" = []
    for store in copies:
        store_id = store_identity(store)
        where = store_id["root"]
        if any(s["root"] == where for s in bound):
            raise Refused(f"{where}: the store copy is bound twice")
        if not store_id["closed"]:
            raise Refused(f"{where}: " + "; ".join(store_id["problems"]))
        if store_id["newestMtime"] >= started_wall:
            raise Refused(f"{where}: the store copy is not older than the capture")
        bound.append(store_id)
    return bound


def _record_cwds(obj) -> list:
    """The working directories one transcript record states (Amendment 13
    O2): a Codex `session_meta` or `turn_context` record's `payload.cwd`,
    and a Claude record's own top-level `cwd` (a Codex record has none, and a
    Claude record is never session_meta or turn_context)."""
    out = []
    if not isinstance(obj, dict):
        return out
    if obj.get("type") in ("session_meta", "turn_context"):
        payload = obj.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
            out.append(payload["cwd"])
    if isinstance(obj.get("cwd"), str):
        out.append(obj["cwd"])
    return out


def scan_cwds(paths, roots, targets=()) -> "tuple[set, dict]":
    """Every working directory recorded in the given FROZEN transcript copies
    (each copy is exactly its captured prefix), streamed line by line. A line
    is parsed only when it names a `"cwd"` key and the spelling of a
    directory root or of a non-traversed directory link's denied target
    (Amendment 14 P1), since only a cwd under a root, or at or below such a
    target, needs a probe; returns (cwds, scan statistics)."""
    needles = set()
    spellings = [r.logical for r in roots if not r.is_file and r.present]
    for logical in spellings + list(targets):
        needles.add(logical.encode("utf-8", "surrogateescape"))
        needles.add(json.dumps(logical)[1:-1].encode("utf-8", "surrogateescape"))
    cwds, files, size, lines, parsed = set(), 0, 0, 0, 0
    started = time.monotonic()
    for path in paths:
        files += 1
        with open(path, "rb") as fh:
            for line in fh:
                lines += 1
                size += len(line)
                if b'"cwd"' not in line or not any(n in line for n in needles):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                parsed += 1
                cwds.update(_record_cwds(obj))
    return cwds, {"files": files, "bytes": size, "lines": lines, "parsedLines": parsed,
                  "seconds": round(time.monotonic() - started, 3)}


def capture(stores, out, *, home: str, codex_home=None, claude_config_dir=None,
            extras=(), retry_deadline_s: float = 120.0, clock=time.monotonic,
            on_copy=None, on_pair=None, now=time.time, probes=()) -> dict:
    """Capture the frozen input set of one measurement session, bound to every
    store copy in `stores` (one path or a list); returns the manifest. Raises
    Refused. `on_copy` and `on_pair` are the test seams of
    `copy_prefix`/`copy_whole` and `capture_sqlite_pair`."""
    started_wall = now()
    store_ids = bind_stores(stores, started_wall)
    freeze = pathlib.Path(out)
    if freeze.exists():
        raise Refused(f"{freeze} exists; capture into a fresh directory")
    home = _norm(home)
    roots = input_roots(home, codex_home, claude_config_dir, extras)
    if _root_of(roots, _norm(out)) is not None:
        raise Refused(f"the freeze {out} lies inside a live root; SQLite may "
                      "open only scratch copies outside every root")
    for root in roots:
        _check_path(root.logical)
        if root.present and os.path.realpath(root.logical) != root.logical:
            raise Refused(f"the root {root.logical} is reached through a symlink "
                          f"({os.path.realpath(root.logical)}); map it by its "
                          "canonical path")
    # The union of every bound copy's retained rows: each prefix reaches the
    # largest cursor or scan target any copy retains, and each copy's
    # retained-missing (and empty) rows are recorded absent.
    retained = [row for s in store_ids for row in retained_rows(s["root"])]
    required: "dict[str, int]" = {}
    for row in retained:
        path = row["path"]
        required[path] = max([required.get(path, 0)]
                             + list(row["offsets"].values()))
    deadline = clock() + retry_deadline_s
    entries: "dict[str, dict]" = {}
    absences: "dict[str, str]" = {}
    exclusions: "list[dict]" = []
    link_roots: "dict[str, Root]" = {}       # out-of-root link target -> its root
    denied: "dict[str, dict]" = {}           # non-traversed dir-link target
    (freeze / "roots").mkdir(parents=True)
    freeze_real = os.path.realpath(freeze)

    def target_root(target: str, link: str) -> Root:
        """The single-file root of an out-of-root link target (Amendment 10):
        mapped like ~/.claude.json, at its own index under roots/."""
        root = link_roots.get(target)
        if root is None:
            root = Root("file", target, None, present=True, index=len(roots),
                        origin="link-target")
            roots.append(root)
            link_roots[target] = root
        if link not in root.links:
            root.links.append(link)
        return root

    def add_dir(root: Root, path: str) -> None:
        if path in entries:
            return
        st = os.lstat(path)
        if stat_mod.S_ISLNK(st.st_mode):
            raise Refused(f"a path through a directory symlink inside a root is "
                          f"refused (the walk never traverses one): {path}")
        target = _frozen(freeze, root, path)
        target.mkdir(parents=True, exist_ok=True)
        entries[path] = {"logical": path, "kind": "dir", "class": "dir",
                         "root": root.index,
                         "physical": str(target.relative_to(freeze)),
                         **_identity(st), "sourceSize": st.st_size,
                         "admittedLength": st.st_size}

    def add_parents(root: Root, path: str) -> None:
        if root.is_file:
            return
        chain = []
        cur = os.path.dirname(path)
        while _under(cur, root.logical):
            chain.append(cur)
            if cur == root.logical:
                break
            cur = os.path.dirname(cur)
        for d in reversed(chain):
            add_dir(root, d)

    def add_sqlite(root: Root, path: str, target: pathlib.Path) -> None:
        """A derived SQLite input (spec §6.3, Q17, `dc14` N1): the namespace
        presents the SOURCE main file's logical path, device, inode and
        timestamps with the DERIVED file's actual length and bytes; source
        and derived provenance are recorded apart, and the derived file's
        sidecars are recorded absent in the namespace."""
        scratch_root = freeze / DERIVE_DIR
        scratch = scratch_root / str(len(entries))
        scratch.mkdir(parents=True)
        try:
            pair = capture_sqlite_pair(path, scratch, deadline=deadline,
                                       clock=clock, on_pair=on_pair)
            length = pair["derived"].stat().st_size
            digest = _sha_file(pair["derived"])
            os.replace(pair["derived"], target)
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)
        physical = str(target.relative_to(freeze))
        sidecars = [path + side for side in SQLITE_SIDECARS]
        for side in sidecars:
            absences[side] = "derived-sidecar"
        entries[path] = {
            "logical": path, "kind": "file", "class": "sqlite-derived",
            "root": root.index, "physical": physical, **pair["identity"],
            "sourceSize": pair["sourceSize"], "admittedLength": length,
            "sha256": digest, "attempts": pair["source"]["attempts"],
            "source": pair["source"],
            "derived": {"path": physical, "length": length, "sha256": digest,
                        "transformation": "the source main file and WAL "
                        "recovered by SQLite on a scratch copy, checkpointed "
                        "(TRUNCATE) and switched to journal_mode=DELETE",
                        "recovery": pair["recovery"],
                        "sidecarsAbsent": sidecars}}

    def add_file(root: Root, path: str, kind: str, need: int = 0) -> None:
        if path in entries:
            return
        _check_path(path)
        add_parents(root, path)
        st = os.lstat(path)
        target = _frozen(freeze, root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.basename(path) in SQLITE_METADATA and kind == "metadata":
            if not stat_mod.S_ISREG(st.st_mode):
                raise Refused(f"a SQLite input that is not a regular file (a "
                              f"symlink?) is refused: {path}")
            add_sqlite(root, path, target)
            return
        if stat_mod.S_ISLNK(st.st_mode):
            # A file link: its target is captured (a retained cursor on the
            # link's own path counts for the target) and the frozen link is a
            # relative link to the frozen copy. A target inside a root is that
            # root's file; one outside every root (Amendment 10) becomes its
            # own single-file root, its live path denied like a root's.
            raw = os.readlink(path)
            resolved = os.path.realpath(path)
            owner = _root_of(roots, resolved)
            need = max(need, required.get(path, 0))
            if owner is not None and owner.origin is None:
                if os.path.isdir(resolved):
                    raise Refused(f"a link to a directory used as a file is "
                                  f"refused: {path} -> {raw}")
                scope = "in-root"
            else:
                resolved = _outside_link_target(path, raw)
                owner = target_root(resolved, path)
                scope = "outside-roots"
            add_file(owner, resolved, kind, need)
            frozen_target = _frozen(freeze, owner, resolved)
            os.symlink(os.path.relpath(frozen_target, target.parent), target)
            entries[path] = {"logical": path, "kind": "symlink", "class": "symlink",
                             "root": root.index, "target": raw,
                             "resolved": resolved, "targetScope": scope,
                             "targetRoot": owner.index,
                             "physical": str(target.relative_to(freeze)),
                             **_identity(st), "sourceSize": st.st_size,
                             "admittedLength": st.st_size}
            return
        if not stat_mod.S_ISREG(st.st_mode):
            raise Refused(f"not a regular file: {path}")
        if kind == "transcript":
            info = copy_prefix(path, str(target), max(need, required.get(path, 0)),
                               deadline=deadline, clock=clock, on_copy=on_copy)
        else:
            info = copy_whole(path, str(target), deadline=deadline, clock=clock,
                              on_copy=on_copy)
        entries[path] = {"logical": path, "kind": "file", "class": kind,
                         "root": root.index,
                         "physical": str(target.relative_to(freeze)), **info}

    def add_dir_link(root: Root, path: str) -> None:
        """A directory link inside a walked root (Amendment 10; the real
        Claude projects hold `<project>/memory` links to repositories'
        .agentmem). The tree's `Path.glob("**/*.jsonl")` never recurses
        through a directory link (CPython 3.14, recurse_symlinks=False), so
        it is covered as NON-TRAVERSED: recorded and presented as a symlink
        (lstat, readlink with the stored target), nothing under it captured,
        and its live target denied by the sandbox profile, so any traversal
        is killed instead of reading live data - the frozen link points at
        that live target, never at frozen bytes. A link named *.jsonl is
        matched by the walk and followed as a file, so it is refused."""
        _check_path(path)
        raw = os.readlink(path)
        if path.endswith(".jsonl"):
            raise Refused(f"a link to a directory used as a file is refused (the "
                          f"walk matches its name): {path} -> {raw}")
        resolved = os.path.realpath(path)
        _check_path(resolved)
        if _under(freeze_real, resolved):
            raise Refused(f"a directory link whose target contains the freeze cannot "
                          f"be denied: {path} -> {raw}")
        # Amendment 13 O1: the target directory's own lstat - never its
        # contents - so the namespace answers a FOLLOWING stat of the link (or
        # a stat of the target itself) without a kernel call on the live
        # target: `os.walk`'s DirEntry.is_dir() follows the link even with
        # followlinks=False, and the frozen link points at the live target.
        try:
            target_st = os.lstat(resolved)
        except OSError as exc:
            raise Refused(f"the target of a non-traversed directory link cannot be "
                          f"stat'ed ({exc.strerror}): {path} -> {raw}") from exc
        add_parents(root, path)
        st = os.lstat(path)
        target = _frozen(freeze, root, path)
        os.symlink(resolved, target)
        entries[path] = {"logical": path, "kind": "symlink", "class": "dir-link",
                         "root": root.index, "target": raw, "resolved": resolved,
                         "traversed": False,
                         "targetScope": ("in-root" if _root_of(roots, resolved)
                                         else "outside-roots"),
                         "physical": str(target.relative_to(freeze)),
                         **_identity(st), "sourceSize": st.st_size,
                         "admittedLength": st.st_size}
        denied.setdefault(resolved, {"logical": resolved, "kind": "dir",
                                     "links": [], "metadata": _meta(target_st)}
                          )["links"].append(path)

    boundary_counts = {}
    for root in list(roots):
        if not root.present:
            absences[root.logical] = "root-absent"
            continue
        if root.is_file:
            add_file(root, root.logical, "metadata")
            continue
        add_dir(root, root.logical)
        files = dirs = 0
        if root.walk is not None:
            add_parents(root, os.path.join(root.walk, "x"))
            for dirpath, dirnames, filenames in os.walk(root.walk):
                dirpath = _norm(dirpath)
                for name in sorted(dirnames):
                    full = os.path.join(dirpath, name)
                    if os.path.islink(full):        # os.walk never descends it
                        add_dir_link(root, full)
                        continue
                    add_dir(root, full)
                    dirs += 1
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    if name.endswith(".jsonl"):
                        add_file(root, full, "transcript")
                        files += 1
                    else:
                        exclusions.append({"logical": full,
                                           "reason": "not a walked *.jsonl"})
            if root.walk != root.logical:
                consumed = {m + side for m in root.metadata
                            if os.path.basename(m) in SQLITE_METADATA
                            for side in SQLITE_SIDECARS}
                for name in sorted(os.listdir(root.logical)):
                    full = os.path.join(root.logical, name)
                    if full != root.walk and full not in root.metadata \
                            and full not in consumed:
                        exclusions.append({"logical": full,
                                           "reason": "outside the walk"})
            else:
                walk_sessions = os.path.join(root.logical, "sessions")
                absences.setdefault(walk_sessions, "no sessions/ directory")
        for meta in root.metadata:
            if os.path.lexists(meta):
                add_file(root, meta, "metadata")
            else:
                absences[meta] = "metadata-absent"
        boundary_counts[root.logical] = {"files": files, "dirs": dirs}
    for extra in extras:
        path = _norm(extra)
        root = _root_of(roots, path)
        if root is None or path in entries:
            continue
        if os.path.lexists(path):
            add_file(root, path, "metadata")
        else:
            absences[path] = "extra-absent"
    boundary_at = now()
    outside = []
    for path in sorted(required):
        root = _root_of(roots, path)
        if root is None:
            outside.append(path)
            continue
        if path in entries:
            continue
        if os.path.lexists(path):
            add_file(root, path, "transcript")
        else:
            absences[path] = "retained-missing"
    # Amendment 13 O2: probe-only entries. The product's git-root walk-up
    # from a recorded working directory (`os.path.realpath`, an lstat per
    # component, then an exists() of each ancestor's `.git`) reads paths
    # under a root that are no input - Codex worktrees under the Codex home,
    # created and removed continuously - so each one is recorded as present
    # (its lstat, and its stat when it is a link) or as a recorded absence,
    # read from the live tree now, metadata only; nothing is copied.
    #
    # Amendment 14 P1: a recorded working directory AT or BELOW the live
    # target of a non-traversed directory link (the real transcripts record
    # a project's `.claude-memory`, a `memory` link's target, as their cwd)
    # gets the same treatment inside that denied target: the target's own
    # `.git`, every component below it and each existing directory's `.git`,
    # as probe entries (`root: null`, `deniedTarget`) or recorded absences.
    # The target itself keeps its O1 presentation (`deniedTargets[].metadata`).
    probe_count = {"entries": 0, "absences": 0,
                   "targetEntries": 0, "targetAbsences": 0}
    # only targets outside every root: one inside a root is that root's
    # directory, which O2's walk already covers (and rootmap.c classifies a
    # path by its root first)
    outside_targets = sorted(t for t in denied if _root_of(roots, t) is None)

    def target_of(path: str) -> "str | None":
        best = None
        for t in outside_targets:
            if _under(path, t) and (best is None or len(t) > len(best)):
                best = t
        return best

    def under_absence(floor: str, path: str) -> bool:
        cur = path
        while _under(cur, floor):
            if cur in absences:
                return True
            if cur == floor:
                return False
            cur = os.path.dirname(cur)
        return False

    def probe_path(floor: str, scope: dict, path: str, origin: str) -> bool:
        """Record one probe path under `floor` (a directory root, or a denied
        target; `scope` names it in the entry); True when the walk may
        descend below it (a real directory)."""
        known = entries.get(path)
        if known is not None:
            return known["kind"] == "dir" or (known["kind"] == "probe"
                                              and known["type"] == "dir")
        if under_absence(floor, path):
            return False
        in_target = "deniedTarget" in scope
        _check_path(path)
        try:
            st = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            absences[path] = "probe-absent"
            probe_count["absences"] += 1
            probe_count["targetAbsences"] += in_target
            return False
        except OSError as exc:
            raise Refused(f"a probe path cannot be stat'ed ({exc.strerror}): "
                          f"{path}") from exc
        entry = {"logical": path, "kind": "probe", "class": "probe",
                 **scope, "probeOf": origin, **_meta(st)}
        if stat_mod.S_ISLNK(st.st_mode):
            # A link: lstat, its stored target (readlink) and what a
            # following stat presents; never descended (the walk-up resolves
            # it first, and its target is probed only if it is under a root).
            entry["target"] = os.readlink(path)
            _check_path(entry["target"])
            try:
                entry["followed"] = _meta(os.stat(path))
                entry["resolved"] = os.path.realpath(path)
                _check_path(entry["resolved"])
            except FileNotFoundError:
                entry["followed"], entry["resolved"] = None, ""
        entries[path] = entry
        probe_count["entries"] += 1
        probe_count["targetEntries"] += "deniedTarget" in scope
        return stat_mod.S_ISDIR(st.st_mode)

    def probe_walk(path: str, *, git: bool, origin: str) -> "str | None":
        """Probe every component of `path` below its directory root, or below
        the denied target it lies at or under (and, with `git`, the floor's
        and each existing directory's `.git`); returns "root" or "target",
        None when `path` lies under neither."""
        root = _root_of(roots, path)
        if root is not None:
            if root.is_file or not root.present:
                return None
            floor, scope, kind = root.logical, {"root": root.index}, "root"
        else:
            floor = target_of(path)
            if floor is None:
                return None
            scope, kind = {"root": None, "deniedTarget": floor}, "target"
        cur = floor
        if git:
            probe_path(floor, scope, os.path.join(cur, ".git"), "git")
        rel = os.path.relpath(path, floor)
        for part in ([] if rel == "." else rel.split(os.sep)):
            cur = os.path.join(cur, part)
            if not probe_path(floor, scope, cur, origin):
                break
            if git:
                probe_path(floor, scope, os.path.join(cur, ".git"), "git")
        return kind

    transcripts = sorted(str(freeze / e["physical"]) for e in entries.values()
                         if e["kind"] == "file" and e["class"] == "transcript")
    cwds, scan = scan_cwds(transcripts, roots, outside_targets)
    probed = target_cwds = 0
    for cwd in sorted(cwds):
        if cwd.startswith("~"):
            cwd = home + cwd[1:]
        if not os.path.isabs(cwd):
            continue
        cwd = os.path.normpath(cwd)
        hits = {probe_walk(cwd, git=True, origin="cwd")}
        canonical = os.path.realpath(cwd)
        if canonical != cwd:
            hits.add(probe_walk(canonical, git=True, origin="cwd-resolved"))
        probed += "root" in hits
        target_cwds += "target" in hits
    explicit = []
    for raw in probes:
        path = _norm(raw)
        if probe_walk(path, git=False, origin="explicit") is None:
            raise Refused(f"a probe path outside every directory root and every "
                          f"denied directory-link target cannot be presented: {raw}")
        explicit.append(path)
    for target, root in link_roots.items():
        root.links.sort()
        entries[target]["linkedFrom"] = list(root.links)
    manifest = {
        "schema": SCHEMA, "home": home,
        "captureStartedAt": _utc(started_wall), "capturedAt": _utc(now()),
        "discoveryBoundary": {"at": _utc(boundary_at), "roots": boundary_counts},
        "stores": store_ids,
        "roots": [{"index": r.index, "kind": r.kind, "logical": r.logical,
                   "walk": r.walk, "present": r.present,
                   "frozen": f"roots/{r.index}",
                   **({"origin": r.origin, "links": list(r.links)} if r.origin
                      else {})} for r in roots],
        "entries": [entries[k] for k in sorted(entries)],
        "absences": [{"logical": k, "reason": v} for k, v in sorted(absences.items())],
        "deniedTargets": [{**denied[k], "links": sorted(denied[k]["links"])}
                          for k in sorted(denied)],
        "exclusions": exclusions,
        "retainedOutsideRoots": outside,
        # cwds: those under a directory root (probed); targetCwds: those at
        # or below a denied directory-link target outside every root (probed
        # inside it, Amendment 14 P1); cwdsParsed: every distinct cwd of a
        # candidate line (one naming "cwd" and a root or target path);
        # entries/absences count both kinds, targetEntries/targetAbsences
        # the ones inside a denied target
        "probes": {"cwds": probed, "targetCwds": target_cwds,
                   "cwdsParsed": len(cwds), **probe_count,
                   "explicit": explicit, "scan": scan},
        "retryDeadlineS": retry_deadline_s,
    }
    _write_controls(freeze, manifest)
    seal = _seal(freeze)
    manifest["seal"] = seal
    return manifest


#: rootmap.tsv version 2 (Amendment 13): an `X` record carries its target's
#: captured metadata. rootmap.c accepts this version only, so a freeze
#: captured before it is refused at activation (recapture it).
ROOTMAP_VERSION = "2"


def _tsv_meta(m: dict, size=None) -> list:
    """dev ino size mode nlink uid gid, four (seconds, nanoseconds) times and
    a reserved 0: the 16 fields rootmap.c reads into one `struct meta`."""
    fields = [m["dev"], m["ino"], m["size"] if size is None else size, m["mode"],
              m["nlink"], m["uid"], m["gid"]]
    for key in ("atimeNs", "mtimeNs", "ctimeNs", "birthtimeNs"):
        fields += [m[key] // 1_000_000_000, m[key] % 1_000_000_000]
    return fields + [0]


def rootmap_tsv(freeze: pathlib.Path, manifest: dict) -> str:
    base = os.path.realpath(freeze)
    lines = ["ROOTMAP\t" + ROOTMAP_VERSION]
    for root in manifest["roots"]:
        lines.append("\t".join(["R", root["logical"],
                                os.path.join(base, root["frozen"]),
                                "1" if root["kind"] == "file" else "0",
                                "1" if root["present"] else "0"]))
    followed = []
    for e in manifest["entries"]:
        kind = {"file": "F", "dir": "D", "symlink": "L", "probe": "P"}[e["kind"]]
        fields = ["E", kind, e["logical"]] + (
            _tsv_meta(e) if kind == "P" else _tsv_meta(e, size=e["admittedLength"]))
        if kind == "L" or (kind == "P" and "target" in e):
            fields += [e["target"], e["resolved"]]
        if kind == "P" and e.get("followed"):
            followed.append(["S", e["logical"]] + _tsv_meta(e["followed"]))
        lines.append("\t".join(str(f) for f in fields))
    # Probe links (Amendment 13 O2): what a FOLLOWING stat presents.
    lines.extend("\t".join(str(f) for f in fields) for fields in followed)
    for a in manifest["absences"]:
        lines.append("\t".join(["A", a["logical"]]))
    # Live targets of non-traversed directory links (Amendments 10 and 13):
    # never mapped or opened, denied by the profile, and presented from their
    # captured metadata alone (a following stat of the link, a stat of the
    # target); anything else on or under one is a refused `traversal`.
    for d in manifest.get("deniedTargets") or []:
        meta = d.get("metadata")
        lines.append("\t".join(str(f) for f in ["X", d["logical"]]
                               + (_tsv_meta(meta) if meta else [])))
    return "\n".join(lines) + "\n"


def _sb_string(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(freeze: pathlib.Path, manifest: dict) -> str:
    """Deny every access to each live logical root (a directory root as a
    subpath, a file root - including an out-of-root link target - as a
    literal), to the live target of each non-traversed directory link (a
    subpath, Amendment 10), and every write to the freeze; a violating
    process is killed, so a call the namespace misses, or a traversal of a
    directory link, fails instead of reading live data (spec §6.3 "Frozen
    namespace")."""
    lines = ["(version 1)", "(allow default)"]
    for root in manifest["roots"]:
        filt = ("literal" if root["kind"] == "file" else "subpath")
        lines.append(f"(deny file-read* file-write* ({filt} "
                     f"{_sb_string(root['logical'])}) (with send-signal SIGKILL))")
    for d in manifest.get("deniedTargets") or []:
        lines.append(f"(deny file-read* file-write* (subpath "
                     f"{_sb_string(d['logical'])}) (with send-signal SIGKILL))")
    lines.append(f"(deny file-write* (subpath {_sb_string(os.path.realpath(freeze))})"
                 " (with send-signal SIGKILL))")
    return "\n".join(lines) + "\n"


def _write_controls(freeze: pathlib.Path, manifest: dict) -> None:
    (freeze / "rootmap.tsv").write_text(rootmap_tsv(freeze, manifest))
    (freeze / "frozen.sb").write_text(sandbox_profile(freeze, manifest))
    manifest["controls"] = {"rootmap.tsv": _sha_file(freeze / "rootmap.tsv"),
                            "frozen.sb": _sha_file(freeze / "frozen.sb")}
    (freeze / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n")


def _walk_physical(base) -> "list[tuple[str, list, list]]":
    """(dirpath, dirnames, others) for every real directory under `base`;
    a symlink is listed in `others` and never stat'ed through or followed.
    A frozen directory link points at its LIVE target (Amendment 10), which
    neither the seal nor verify may touch (os.walk stats through links)."""
    out, stack = [], [str(base)]
    while stack:
        top = stack.pop()
        dirs, others = [], []
        with os.scandir(top) as it:
            for entry in it:
                (dirs if entry.is_dir(follow_symlinks=False) else others).append(
                    entry.name)
        dirs.sort()
        others.sort()
        out.append((top, dirs, others))
        stack.extend(os.path.join(top, d) for d in reversed(dirs))
    return out


def _frozen_paths(freeze: pathlib.Path):
    base = freeze / "roots"
    for dirpath, dirnames, others in _walk_physical(base):
        for name in dirnames + others:
            full = pathlib.Path(dirpath) / name
            yield str(full.relative_to(freeze)), full
    yield "roots", base


def _meta_line(rel: str, full: pathlib.Path) -> str:
    st = os.lstat(full)
    kind = ("L" if stat_mod.S_ISLNK(st.st_mode) else
            "D" if stat_mod.S_ISDIR(st.st_mode) else "F")
    size = st.st_size if kind == "F" else 0
    return f"{rel}\t{kind}\t{size}\t{stat_mod.S_IMODE(st.st_mode):o}\t" \
           f"{st.st_mtime_ns}\t{st.st_ino}\n"


def _digests(freeze: pathlib.Path, *, full: bool) -> dict:
    meta = hashlib.sha256()
    content = hashlib.sha256()
    files = total = 0
    for rel, path in sorted(_frozen_paths(freeze)):
        meta.update(_meta_line(rel, path).encode("utf-8", "surrogateescape"))
        if path.is_symlink() or not path.is_file():   # never through a link
            continue
        if full:
            content.update(f"{rel}\t{_sha_file(path)}\n".encode(
                "utf-8", "surrogateescape"))
        files += 1
        total += path.stat().st_size
    out = {"metaDigest": meta.hexdigest(), "files": files, "bytes": total}
    if full:
        out["contentDigest"] = content.hexdigest()
    return out


#: Finder metadata macOS may create inside a freeze directory while the copy
#: runs (the first real capture gained six 6,148-byte `.DS_Store` files); never
#: an input, so the seal removes it rather than freezing it.
FINDER_METADATA = ".DS_Store"


def _seal(freeze: pathlib.Path) -> dict:
    manifest = json.loads((freeze / "manifest.json").read_text())
    physical = {e["physical"] for e in manifest["entries"] if e.get("physical")}
    removed = 0
    for dirpath, _dirnames, others in reversed(_walk_physical(freeze / "roots")):
        for name in list(others):
            full = os.path.join(dirpath, name)
            if os.path.relpath(full, freeze) in physical:
                continue
            if name == FINDER_METADATA and not os.path.islink(full):
                os.remove(full)
                others.remove(name)
                removed += 1
                continue
            raise Refused(f"an unmanifested file in the freeze: {full}")
        for name in others:
            full = os.path.join(dirpath, name)
            if not os.path.islink(full):
                os.chmod(full, 0o444)
        os.chmod(dirpath, 0o555)
    for name in CONTROL_FILES:
        os.chmod(freeze / name, 0o444)
    seal = {"schema": SEAL_SCHEMA, "sealedAt": _utc(),
            "removedFinderMetadata": removed,
            "controls": {name: _sha_file(freeze / name) for name in CONTROL_FILES},
            **_digests(freeze, full=True)}
    (freeze / "seal.json").write_text(json.dumps(seal, indent=1, sort_keys=True) + "\n")
    os.chmod(freeze / "seal.json", 0o444)
    os.chmod(freeze, 0o555)
    return seal


def seal_identity(freeze) -> dict:
    """{sealSha256, manifestSha256, rootmapSha256, profileSha256}: what a
    receipt binds to name this freeze."""
    freeze = pathlib.Path(freeze)
    return {"freeze": os.path.realpath(freeze),
            "sealSha256": _sha_file(freeze / "seal.json"),
            "manifestSha256": _sha_file(freeze / "manifest.json"),
            "rootmapSha256": _sha_file(freeze / "rootmap.tsv"),
            "profileSha256": _sha_file(freeze / "frozen.sb")}


def verify(freeze, *, full: bool = False) -> dict:
    freeze = pathlib.Path(freeze)
    problems = []
    try:
        seal = json.loads((freeze / "seal.json").read_text())
    except (OSError, ValueError) as exc:
        return {"valid": False, "problems": [f"no readable seal: {exc}"]}
    for name in CONTROL_FILES:
        try:
            got = _sha_file(freeze / name)
        except OSError as exc:
            problems.append(f"{name}: {exc}")
            continue
        if got != (seal.get("controls") or {}).get(name):
            problems.append(f"{name} changed since the seal")
    if os.access(freeze, os.W_OK):
        problems.append("the freeze directory is writable")
    now = _digests(freeze, full=full)
    for key in ("metaDigest", "files", "bytes") + (("contentDigest",) if full else ()):
        if now.get(key) != seal.get(key):
            problems.append(f"{key} differs from the seal "
                            f"({now.get(key)} vs {seal.get(key)})")
    return {"valid": not problems, "full": full, "problems": problems,
            "seal": seal_identity(freeze) if not problems else None,
            "verifiedAt": _utc()}


# ── qualification of a (store copy, freeze) pair ─────────────────────────

def _prep_residual(store) -> "tuple[set, str | None]":
    path = pathlib.Path(store) / "prep-receipt.json"
    if not path.exists():
        return set(), None
    try:
        prep = json.loads(path.read_text())
    except ValueError as exc:
        return set(), f"prep-receipt.json unreadable: {exc}"
    if prep.get("valid") is not True:
        return set(), "prep-receipt.json is not valid"
    return set(prep.get("residual") or []), None


def bound_stores(manifest: dict) -> "list[dict]":
    """Every store copy identity a freeze's manifest is bound to: `stores`
    (frozen-roots/2), or a pre-Amendment-11 freeze's single `store`."""
    if manifest.get("stores") is not None:
        return list(manifest["stores"])
    return [manifest["store"]] if manifest.get("store") else []


def qualify(store, freeze) -> dict:
    """Qualify ONE store copy against a freeze bound to it (Amendment 11: run
    once per bound copy; the refusal rule applies per copy)."""
    freeze = pathlib.Path(freeze)
    manifest = json.loads((freeze / "manifest.json").read_text())
    entries = {e["logical"]: e for e in manifest["entries"]}
    absences = {a["logical"]: a["reason"] for a in manifest["absences"]}
    roots = [Root(r["kind"], r["logical"], r["walk"], present=r["present"],
                  index=r["index"]) for r in manifest["roots"]]
    store_id = store_identity(store)
    problems = list(store_id["problems"])
    bound = bound_stores(manifest)
    captured = next((s for s in bound if s.get("root") == store_id["root"]), None)
    if captured is None:
        problems.append(f"{store_id['root']} is not a store copy this freeze is "
                        "bound to (bound: "
                        + (", ".join(str(s.get("root")) for s in bound) or "none")
                        + "); capture one freeze with --store for every copy")
    else:
        for name in DATABASES:
            a = (store_id["databases"].get(name) or {})
            b = (captured.get("databases") or {}).get(name) or {}
            if {k: a.get(k) for k in ("size", "mtimeNs", "userVersion")} != \
                    {k: b.get(k) for k in ("size", "mtimeNs", "userVersion")}:
                problems.append(f"{name} is not the store copy the freeze was "
                                "captured against")
    residual, prep_error = _prep_residual(store)
    if prep_error:
        problems.append(prep_error)
    seal_check = verify(freeze)
    problems.extend(f"seal: {p}" for p in seal_check["problems"])
    checked = residual_used = prefixes = empty_absent = 0
    for row in retained_rows(store):
        path = row["path"]
        root = _root_of(roots, path)
        if root is None:
            continue
        checked += 1
        where = f"{row['store']}.{row['table']}: {path}"
        entry = entries.get(path)
        if entry is not None and entry["kind"] == "symlink":
            entry = entries.get(entry["resolved"])
        if entry is not None and entry["kind"] == "probe":
            entry = None                      # metadata only: no frozen bytes
        if entry is None:
            reason = absences.get(path)
            if reason is not None and path in residual:
                residual_used += 1
                continue
            # A row with zero size and a zero cursor (recorded while the file
            # was empty, the file later deleted) retains no byte the freeze
            # could contradict; catchup.py's preparation counts only positive
            # sizes as tracked for the same reason. The capture must still
            # have recorded it absent, so the namespace presents ENOENT.
            if reason is not None and not any(row["offsets"].values()):
                empty_absent += 1
                continue
            problems.append(f"{where} is retained but "
                            + (f"absent from the freeze ({reason})" if reason
                               else "not in the freeze"))
            continue
        for col, value in row["offsets"].items():
            if value > entry["admittedLength"]:
                problems.append(f"{where}: retained {col} {value} is beyond the "
                                f"frozen file ({entry['admittedLength']} bytes)")
        inode = row["identity"].get("inode")
        if inode is not None and int(inode) != int(entry["ino"]):
            problems.append(f"{where}: replaced (stored inode {inode}, captured "
                            f"{entry['ino']})")
        digest = row["identity"].get("committed_prefix_sha256")
        committed = row["offsets"].get("last_byte_offset", 0)
        if digest and committed <= entry["admittedLength"]:
            prefixes += 1
            got = _sha_file(freeze / entry["physical"], committed)
            if got != digest:
                problems.append(f"{where}: the committed prefix ({committed} "
                                "bytes) differs from the frozen file's")
    return {"schema": QUALIFY_SCHEMA, "qualified": not problems,
            "problems": problems, "store": store_id,
            "boundStores": [s.get("root") for s in bound],
            "freeze": seal_identity(freeze), "retainedChecked": checked,
            "committedPrefixesVerified": prefixes,
            "residualAdmitted": residual_used,
            "retainedEmptyAbsent": empty_absent, "qualifiedAt": _utc()}


# ── the run family's namespace receipts ───────────────────────────────────

def load_receipts(directory) -> "dict[str, list]":
    out = {}
    directory = pathlib.Path(directory)
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.jsonl")):
        if path.name in (LAUNCHES, TEARDOWN, TOPLEVEL):
            continue
        lines = []
        for raw in path.read_text(errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except ValueError:
                lines.append({"event": "malformed", "raw": raw[:200]})
        out[path.name] = lines
    return out


def _launches(directory) -> list:
    path = pathlib.Path(directory) / LAUNCHES
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _system_image(event: dict) -> bool:
    """A launch whose image is a protected system program: a system binary,
    or a "#!" script whose interpreter is one (npm's /usr/bin/env)."""
    target = str(event.get("target") or "")
    interpreter = str(event.get("interpreter") or "")
    return target.startswith(SYSTEM_PREFIXES) or interpreter.startswith(SYSTEM_PREFIXES)


def _teardown_kills(directory) -> "tuple[set, list]":
    """{(pid, signal)} the harness recorded killing at teardown (`reap`), and
    the problems of an unreadable record."""
    path = pathlib.Path(directory) / TEARDOWN
    kills, problems = set(), []
    if not path.exists():
        return kills, problems
    for raw in path.read_text(errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            kills.add((int(row["pid"]), int(row["signal"])))
        except (ValueError, KeyError, TypeError):
            problems.append(f"{TEARDOWN}: a malformed teardown record")
    return kills, problems


def _int(value) -> "int | None":
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def toplevel_record(pid: int, *, runner: str, step: str, status=None,
                    returncode=None) -> dict:
    """One toplevel/1 row from a shell wait status (above 128: signal
    STATUS-128) or a Python returncode (negative: that signal)."""
    if returncode is not None:
        signaled = returncode < 0
        sig, code = (-returncode, None) if signaled else (None, returncode)
        status = 128 - returncode if signaled else returncode
    else:
        signaled = status > 128
        sig, code = (status - 128, None) if signaled else (None, status)
    return {"schema": TOPLEVEL_SCHEMA, "pid": int(pid), "status": status,
            "exited": not signaled, "code": code, "signaled": signaled,
            "signal": sig, "runner": runner, "step": step, "t": time.time()}


def record_toplevel(directory, pid: int, *, runner: str, step: str, status=None,
                    returncode=None) -> dict:
    """Append how a top-level family process ended to DIR/toplevel.jsonl."""
    row = toplevel_record(pid, runner=runner, step=step, status=status,
                          returncode=returncode)
    os.makedirs(directory, exist_ok=True)
    with open(pathlib.Path(directory) / TOPLEVEL, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def record_teardown(directory, pid: int, signal: int, *, by: str,
                    group: bool = False) -> None:
    """The harness's own kill of a family process, recorded BEFORE it is sent
    (as `reap` and `_inputs.sh` wa_kill do)."""
    os.makedirs(directory, exist_ok=True)
    with open(pathlib.Path(directory) / TEARDOWN, "a") as fh:
        fh.write(json.dumps({"pid": int(pid), "signal": int(signal), "group": group,
                             "t": time.time(), "by": by}) + "\n")


def _toplevel_ends(directory) -> "tuple[dict, list]":
    """{pid: [toplevel rows]} and the problems of an unreadable row."""
    path = pathlib.Path(directory) / TOPLEVEL
    ends, problems = {}, []
    if not path.exists():
        return ends, problems
    for raw in path.read_text(errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            pid = _int(row["pid"])
            if pid is None or not isinstance(row.get("signaled"), bool) or (
                    row["signaled"] and _int(row.get("signal")) is None):
                raise ValueError(raw)
        except (ValueError, KeyError, TypeError, AttributeError):
            problems.append(f"{TOPLEVEL}: a malformed top-level end record")
            continue
        ends.setdefault(pid, []).append(row)
    return ends, problems


def _receipt_pid(name: str, lines: list) -> "int | None":
    """The pid a receipt file belongs to: its name (<pid>.<s>.<us>.jsonl),
    else its first activation line."""
    head = name.split(".", 1)[0]
    if head.isdigit():
        return int(head)
    for line in lines:
        if line.get("event") == "activate":
            return _int(line.get("pid"))
    return None


def activation_ready(text: str) -> bool:
    """HR-10: a receipt's process is ready for a teardown kill only once a
    complete `activate` line was written after its last `exec` (an
    `exec-failed` returns to the image that was already active). A torn
    last line - no newline yet - is never ready."""
    if not text.endswith("\n"):
        return False
    active, before_exec = False, False
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            active = False
            continue
        event = line.get("event")
        if event == "activate" and isinstance(line.get("enforced"), list):
            active = True
        elif event == "exec":
            before_exec, active = active, False
        elif event == "exec-failed":
            active = before_exec
    return active


def family_verdict(directory, rootmap_sha: str, *, expect_pids=()) -> dict:
    """Pure over the receipts directory: is every process of the family
    covered by the namespace of the freeze whose rootmap.tsv hashes to
    `rootmap_sha`, every process started through a protected system program
    admitted by its launch record and recorded termination (Q17), no family
    member ended by a signal the harness did not record, and (Amendment 19
    HR-3) the end of every receipt's process on record: its own exit line, a
    family member's reaped record, a teardown record or the runner's
    top-level record - for an exec into an admitted system program, its
    parent's reaped record of that pid?"""
    files = load_receipts(directory)
    problems = []
    activated = set()
    images = 0
    system_launches = []                 # (receipt name, launch line, via)
    reaped: "dict[int, list]" = {}       # child pid -> its recorded terminations
    sent: "dict[int, list]" = {}         # target pid -> family signals sent to it
    for name, lines in files.items():
        last_exec = None
        for line in lines:
            event = line.get("event")
            pid = line.get("pid")
            if event == "activate":
                images += 1
                activated.add(pid)
                if line.get("schema") != RECEIPT_SCHEMA:
                    problems.append(f"{name}: receipt schema {line.get('schema')}")
                if line.get("ok") is not True:
                    problems.append(f"{name}: the namespace did not load "
                                    f"({line.get('error')})")
                if line.get("manifestSha256") != rootmap_sha:
                    problems.append(f"{name}: loaded a different manifest "
                                    f"({line.get('manifestSha256')})")
                enforced = line.get("enforced") or []
                if not enforced or not all(e.get("denied") for e in enforced):
                    problems.append(f"{name}: the sandbox does not deny every "
                                    "live root (absent enforcement)")
                last_exec = None
            elif event in ("unmapped", "unmapped-dirent", "unmapped-fd",
                           "unmapped-open", "traversal", "write-denied"):
                problems.append(f"{name}: {event} {line.get('call')} "
                                f"{line.get('path')}")
            elif event == "exec":
                last_exec = line
            elif event == "exec-failed":
                last_exec = None
            elif event == "spawn":
                target = line.get("target") or ""
                child = line.get("child")
                if _system_image(line):
                    system_launches.append((name, line, "spawn"))
                elif child is not None and not any(
                        n.startswith(f"{child}.") for n in files):
                    problems.append(f"{name}: spawned {target} (pid {child}) "
                                    "without an activation receipt")
            elif event == "reaped":
                child = _int(line.get("child"))
                if child is None:
                    problems.append(f"{name}: a termination record without a "
                                    "child pid")
                else:
                    reaped.setdefault(child, []).append(line)
            elif event == "signal-sent":
                target = _int(line.get("target"))
                if target is not None:
                    sent.setdefault(target, []).append(line)
            elif event == "malformed":
                problems.append(f"{name}: a malformed receipt line")
            elif event == "exit":
                dropped = (line.get("counters") or {}).get("eventsDropped")
                if dropped:
                    problems.append(f"{name}: {dropped} receipt events dropped")
        if last_exec is not None:
            if _system_image(last_exec):
                system_launches.append((name, last_exec, "exec"))
            else:
                problems.append(f"{name}: exec of {last_exec.get('target')} "
                                "without a later activation (a lost receipt)")
    teardown, teardown_problems = _teardown_kills(directory)
    problems.extend(teardown_problems)
    # Any signal termination ends the run (a sandbox kill sends SIGKILL and
    # logs nothing), unless the harness recorded that kill at teardown or a
    # family member's own recorded kill/killpg sent that signal to that child
    # (the product's subprocess timeouts, such as `npm prefix -g` after 2 s).
    # A sandbox kill has no sender, so it stays fatal.
    teardown_kills, signal_terminations, family_kills = 0, [], []
    for child in sorted(reaped):
        for sig in sorted({r.get("signal") for r in reaped[child]
                           if r.get("signaled") is True}, key=str):
            if (child, sig) in teardown:
                teardown_kills += 1
                continue
            senders = [x for x in sent.get(child, []) if x.get("signal") == sig]
            if senders:
                family_kills.append({
                    "pid": child, "signal": sig,
                    "reapedBy": next(r.get("pid") for r in reaped[child]
                                     if r.get("signal") == sig),
                    "sentBy": senders[0].get("pid")})
                continue
            who = (f"sent by family pid {senders[0].get('pid')} "
                   f"({senders[0].get('call')})" if senders else
                   "no family member sent it: a sandbox kill or an external "
                   "signal")
            waiter = next(r.get("pid") for r in reaped[child]
                          if r.get("signal") == sig)
            signal_terminations.append({"pid": child, "signal": sig,
                                        "reapedBy": waiter,
                                        "sentBy": senders[0].get("pid")
                                        if senders else None})
            problems.append(f"pid {child} (reaped by pid {waiter}) was "
                            f"terminated by signal {sig}, unexpectedly ({who})")
    # Amendment 13 O3: a top-level family process's own end, recorded by the
    # runner (its parent, the runner's shell, does not load the namespace),
    # under the same teardown and familyKills rules as a member's.
    toplevel, top_problems = _toplevel_ends(directory)
    problems.extend(top_problems)
    judged = {(c, r.get("signal")) for c in reaped for r in reaped[c]
              if r.get("signaled") is True}
    top_signaled = 0
    for pid in sorted(toplevel):
        rows = toplevel[pid]
        for sig in sorted({r["signal"] for r in rows if r["signaled"]}):
            top_signaled += 1
            if (pid, sig) in judged:
                continue
            row = next(r for r in rows if r["signaled"] and r["signal"] == sig)
            where = {"runner": row.get("runner"), "step": row.get("step")}
            if (pid, sig) in teardown:
                teardown_kills += 1
                continue
            senders = [x for x in sent.get(pid, []) if x.get("signal") == sig]
            if senders:
                family_kills.append({"pid": pid, "signal": sig, "reapedBy": None,
                                     "sentBy": senders[0].get("pid"), "topLevel": where})
                continue
            signal_terminations.append({"pid": pid, "signal": sig, "reapedBy": None,
                                        "sentBy": None, "topLevel": where})
            problems.append(f"top-level pid {pid} ({where['runner']} {where['step']}) "
                            f"was terminated by signal {sig}, unexpectedly (no "
                            "family member sent it and the harness recorded no "
                            "teardown kill: a sandbox kill or an external signal)")
    admitted = []
    for name, line, via in system_launches:
        target = line.get("target")
        argv = line.get("argv")
        if via == "spawn":
            parent, child = _int(line.get("pid")), _int(line.get("child"))
        else:
            parent, child = _int(line.get("ppid")), _int(line.get("pid"))
        what = f"{name}: the system program {target} ({via}"
        if not (target and isinstance(argv, list) and argv
                and parent is not None and child is not None):
            problems.append(f"{what}) without a complete launch record (program, "
                            "arguments, parent and child pid)")
            continue
        ends = [r for r in reaped.get(child, []) if r.get("pid") == parent]
        torn = sorted(sig for pid, sig in teardown if pid == child)
        if not ends and not torn:
            problems.append(f"{what}, pid {child}) without a recorded "
                            "termination status")
            continue
        # HR-21: a program still running at teardown was killed with its
        # group; the per-member teardown record is its termination.
        termination = ({k: ends[-1].get(k) for k in
                        ("exited", "code", "signaled", "signal")}
                       if ends else {"teardown": True, "signal": torn[-1]})
        admitted.append({"program": target,
                         "interpreter": line.get("interpreter"), "argv": argv,
                         "parent": parent, "child": child, "via": via,
                         "termination": termination})
    launched = [int(x["pid"]) for x in _launches(directory)]
    for pid in sorted(set(launched) | {int(p) for p in expect_pids}):
        if pid not in activated:
            problems.append(f"pid {pid} was launched in the family but never "
                            "activated the namespace (a lost receipt)")
    # Every process the launch contract started (launches.jsonl) is a
    # top-level process whose end someone recorded; a runner path that ends
    # one unrecorded would hide its kill again (Amendment 13 O3).
    unrecorded = sorted({p for p in launched if p not in toplevel and p not in reaped})
    for pid in unrecorded:
        problems.append(f"pid {pid} was launched as a top-level family process but "
                        f"how it ended was not recorded ({TOPLEVEL})")
    if not files:
        problems.append("no namespace receipts at all")
    # HR-3: how every receipt's process ended. A detached worker the sandbox
    # killed is reaped by launchd and leaves only its activation.
    teardown_pids = {pid for pid, _sig in teardown}
    unterminated = []
    for name, lines in sorted(files.items()):
        pid = _receipt_pid(name, lines)
        if any(line.get("event") == "exit" for line in lines) or (
                pid is not None and (pid in reaped or pid in teardown_pids
                                     or pid in toplevel)):
            continue
        unterminated.append(pid)
        problems.append(f"{name}: pid {pid} has no termination evidence (no "
                        "exit line, no family member's reaped record, no "
                        "teardown record and no top-level record)")
    return {"schema": FAMILY_SCHEMA, "valid": not problems,
            "problems": problems[:200], "problemCount": len(problems),
            "processes": len(files), "images": images,
            "unloadedSystemImages": len(admitted),
            "admittedSystemPrograms": admitted[:200],
            "terminations": sum(len(v) for v in reaped.values()),
            "teardownKills": teardown_kills,
            "familyKills": family_kills[:50],
            "signalTerminations": signal_terminations[:50],
            "topLevel": {"records": sum(len(v) for v in toplevel.values()),
                         "signaled": top_signaled, "unrecorded": unrecorded[:50]},
            "launched": len(launched), "rootmapSha256": rootmap_sha,
            "unterminated": unterminated[:50]}


def family(directory, freeze, *, expect_pids=()) -> dict:
    sha = _sha_file(pathlib.Path(freeze) / "rootmap.tsv")
    return family_verdict(directory, sha, expect_pids=expect_pids)


# ── the namespace guard, reaping, and the run's input receipt ─────────────

INPUTS_SCHEMA = "write-attribution-inputs/1"


def frozen_freeze() -> "str | None":
    """FREEZE when this process belongs to a frozen run, else None."""
    raw = os.environ.get("WRITE_ATTRIBUTION_INPUTS", "")
    return raw[len("frozen:"):] if raw.startswith("frozen:") else None


def namespace_active() -> bool:
    import ctypes
    try:
        return bool(ctypes.CDLL(None).rootmap_active())
    except (AttributeError, OSError):
        return False


def require_namespace(tool: str) -> None:
    """A harness process of a frozen run (catch-up, hook runner, copier, P1,
    a maintenance operation) refuses unless the namespace of THAT freeze is
    loaded in it: started without the launch contract it would read the live
    roots, so its family would be invalid (spec §6.3 "Frozen namespace")."""
    freeze = frozen_freeze()
    if freeze is None:
        return
    manifest = os.environ.get("ROOTMAP_MANIFEST", "")
    same = bool(manifest) and os.path.dirname(os.path.realpath(manifest)) \
        == os.path.realpath(freeze)
    if not (namespace_active() and same):
        print(f"{tool}: refusing: WRITE_ATTRIBUTION_INPUTS is frozen:{freeze} but "
              "this process was not started through the frozen namespace "
              "(sandbox-exec + frozen_launch.py)", file=sys.stderr)
        raise SystemExit(2)


def _start_second(pid: int) -> "int | None":
    import subprocess
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = " ".join(out.stdout.split())
    if not text:
        return None
    try:
        return int(time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y")))
    except ValueError:
        return None


def reap(directory) -> dict:
    """Bounded teardown of a frozen family: SIGKILL every process still alive
    whose pid AND start second match a receipt file name
    (<pid>.<start_s>.<start_us>.jsonl), so a reused pid is never touched.
    Each kill is first recorded in DIR/teardown.jsonl, so a family member's
    recorded wait that sees it is the harness's teardown, not an unexpected
    signal (spec §6.3 "Frozen namespace", Q17)."""
    import signal
    killed, checked = [], 0
    for path in sorted(pathlib.Path(directory).glob("*.jsonl")):
        parts = path.name.split(".")
        if len(parts) != 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, start = int(parts[0]), int(parts[1])
        if pid in (os.getpid(), os.getppid()):
            continue
        checked += 1
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        if _start_second(pid) != start:
            continue
        with open(pathlib.Path(directory) / TEARDOWN, "a") as fh:
            fh.write(json.dumps({"pid": pid, "start": start,
                                 "signal": int(signal.SIGKILL), "t": time.time(),
                                 "by": "frozen_roots.reap"}) + "\n")
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            pass
    return {"checked": checked, "killed": killed}


def _lib(path) -> "dict | None":
    if not path or not os.path.exists(path):
        return None
    return {"path": os.path.realpath(path), "sha256": _sha_file(path)}


def _read_json(path) -> "dict | None":
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return None


def host_identity() -> dict:
    """The host a run measured on (Amendment 19 HR-21): its name, hardware
    model, OS release and build, and architecture - what a pair must match."""
    import platform
    import socket
    import subprocess

    def sysctl(name):
        try:
            return subprocess.run(["sysctl", "-n", name], capture_output=True,
                                  text=True, timeout=10).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return {"name": socket.gethostname(), "model": sysctl("hw.model"),
            "os": platform.mac_ver()[0] or platform.release(),
            "build": sysctl("kern.osversion"), "machine": platform.machine()}


def inputs_begin(run, *, wtrace, rootmap) -> dict:
    """OUT/inputs.json at the start of a run: the input mode and the
    identities a verdict binds (runtime, host, both libraries, the sandbox
    profile, the freeze, the namespace self-test and the seal before the
    run)."""
    run = pathlib.Path(run)
    freeze = frozen_freeze()
    receipt = {"schema": INPUTS_SCHEMA, "createdAt": _utc(),
               "mode": "frozen" if freeze else "live",
               "runtime": {"python": "/opt/homebrew/bin/python3",
                           "version": sys.version.split()[0]},
               "host": host_identity(),
               "libraries": {"wtrace": _lib(wtrace)}}
    if freeze:
        selftest = _read_json(run / "frozen-selftest.json") or {}
        receipt["libraries"]["rootmap"] = _lib(rootmap)
        receipt["freeze"] = seal_identity(freeze)
        receipt["frozenSelftest"] = {
            "passed": selftest.get("passed") is True,
            "checks": len(selftest.get("checks") or []),
            "rootmapSha256": (selftest.get("rootmap") or {}).get("sha256"),
            "wtraceSha256": (selftest.get("wtrace") or {}).get("sha256"),
            "python": selftest.get("python"), "tree": selftest.get("tree")}
        receipt["verifyBefore"] = _read_json(run / "freeze-verify-before.json")
    (run / "inputs.json").write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    return receipt


def inputs_finish(run) -> "list[str]":
    run = pathlib.Path(run)
    receipt = _read_json(run / "inputs.json") or {}
    receipt["verifyAfter"] = _read_json(run / "freeze-verify-after.json")
    family_result = _read_json(run / "frozen-family.json")
    receipt["family"] = None if family_result is None else {
        k: family_result.get(k) for k in ("valid", "problems", "problemCount",
                                          "processes", "images",
                                          "unloadedSystemImages",
                                          "admittedSystemPrograms",
                                          "teardownKills", "familyKills",
                                          "signalTerminations", "topLevel",
                                          "launched", "rootmapSha256")}
    (run / "inputs.json").write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    return check_inputs(run)


def check_inputs(run, *, require_frontier=False) -> "list[str]":
    """Why a run's evidence cannot carry a revision-15 verdict ([] when it
    can). Frozen: the namespace self-test passed with THESE libraries and
    runtime, the freeze verified before and after with one seal identity, and
    the family's activation receipts (recomputed here from OUT/rootmap) are
    complete and enforced for this freeze. Live: the finite-frontier receipt
    of the drained admission when the run had one (or `require_frontier`)."""
    run = pathlib.Path(run)
    receipt = _read_json(run / "inputs.json")
    if receipt is None:
        return ["no input-mode receipt (inputs.json): a revision-15 verdict "
                "needs the input mode"]
    mode = receipt.get("mode")
    problems = []
    if mode == "live":
        catchup = _read_json(run / "catchup.json")
        frontier = (catchup or {}).get("frontier")
        if (catchup is not None or require_frontier) and not (
                isinstance(frontier, dict) and frontier.get("mode") == "live"):
            problems.append("a live run without its finite-frontier receipt "
                            "(catchup.json frontier)")
        return problems
    if mode != "frozen":
        return [f"unknown input mode {mode!r}"]
    libs = receipt.get("libraries") or {}
    self_test = receipt.get("frozenSelftest") or {}
    if self_test.get("passed") is not True:
        problems.append("the namespace self-test did not pass for this run")
    for name, key in (("rootmap", "rootmapSha256"), ("wtrace", "wtraceSha256")):
        have = (libs.get(name) or {}).get("sha256")
        if not have or have != self_test.get(key):
            problems.append(f"mismatched identities: the {name} library "
                            f"{have} is not the one the self-test passed "
                            f"({self_test.get(key)})")
    if self_test.get("python") != (receipt.get("runtime") or {}).get("version"):
        problems.append("mismatched identities: the self-test ran on another "
                        "runtime")
    freeze = receipt.get("freeze") or {}
    for when in ("verifyBefore", "verifyAfter"):
        result = receipt.get(when)
        if not isinstance(result, dict) or result.get("valid") is not True:
            problems.append(f"the freeze did not verify ({when})")
        elif (result.get("seal") or {}).get("sealSha256") != freeze.get("sealSha256") \
                or (result.get("seal") or {}).get("rootmapSha256") != freeze.get("rootmapSha256"):
            problems.append(f"the freeze changed ({when} names another seal)")
    expect = []
    if (run / "pid").exists():
        try:
            expect.append(int((run / "pid").read_text().strip()))
        except ValueError:
            problems.append("an unreadable pid file")
    fam = family_verdict(run / "rootmap", freeze.get("rootmapSha256") or "",
                         expect_pids=expect)
    problems.extend(f"family: {p}" for p in fam["problems"])
    root_path = (libs.get("rootmap") or {}).get("path")
    for lines in load_receipts(run / "rootmap").values():
        for line in lines:
            if line.get("event") == "activate" and root_path \
                    and line.get("library") != root_path:
                problems.append("mismatched identities: a process loaded "
                                f"{line.get('library')}, not {root_path}")
                break
    return problems


def evidence_label(run) -> dict:
    """{mode, freeze} for a verdict's receipt: frozen evidence names its
    seal; live evidence is labelled live."""
    receipt = _read_json(pathlib.Path(run) / "inputs.json") or {}
    return {"mode": receipt.get("mode"),
            "freeze": (receipt.get("freeze") or {}).get("sealSha256")}


# ── CLI ───────────────────────────────────────────────────────────────────

def _emit(result: dict, out, ok_word: str, bad_word: str, ok: bool) -> int:
    text = json.dumps(result, indent=1, sort_keys=True, default=str) + "\n"
    if out:
        pathlib.Path(out).write_text(text)
    problems = result.get("problems") or []
    print(ok_word if ok else f"{bad_word}: " + "; ".join(problems[:10]))
    return 0 if ok else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--store", required=True, action="append",
                   help="a closed store copy the freeze serves; repeat it for "
                   "every copy of the measurement session")
    c.add_argument("--out", required=True)
    c.add_argument("--home", default=os.environ.get("HOME"))
    c.add_argument("--codex-home", default=os.environ.get("CODEX_HOME"))
    c.add_argument("--claude-config-dir",
                   default=os.environ.get("CLAUDE_CONFIG_DIR"))
    c.add_argument("--extra", action="append", default=[])
    c.add_argument("--probe", action="append", default=[],
                   help="a path under a directory root the family only checks "
                   "(lstat/stat/access): recorded as metadata, never copied")
    c.add_argument("--retry-deadline-s", type=float, default=120.0)
    v = sub.add_parser("verify")
    v.add_argument("--freeze", required=True)
    v.add_argument("--full", action="store_true")
    v.add_argument("--out")
    q = sub.add_parser("qualify")
    q.add_argument("--store", required=True)
    q.add_argument("--freeze", required=True)
    q.add_argument("--out")
    f = sub.add_parser("family")
    f.add_argument("--receipts", required=True)
    f.add_argument("--freeze", required=True)
    f.add_argument("--expect-pid", action="append", default=[])
    f.add_argument("--out")
    r = sub.add_parser("reap")
    r.add_argument("--receipts", required=True)
    rd = sub.add_parser("ready", help="exit 0 once PID's receipt holds a "
                        "complete activation after its last exec (HR-10)")
    rd.add_argument("--receipts", required=True)
    rd.add_argument("--pid", required=True, type=int)
    i = sub.add_parser("inputs")
    i.add_argument("--run", required=True)
    i.add_argument("--begin", action="store_true")
    i.add_argument("--finish", action="store_true")
    i.add_argument("--wtrace")
    i.add_argument("--rootmap")
    args = parser.parse_args(argv)
    if args.cmd == "reap":
        print(json.dumps(reap(args.receipts)))
        return 0
    if args.cmd == "ready":
        texts = [p.read_text(errors="replace") for p in
                 sorted(pathlib.Path(args.receipts).glob(f"{args.pid}.*.jsonl"))]
        return 0 if texts and activation_ready(texts[-1]) else 1
    if args.cmd == "inputs":
        if args.begin:
            receipt = inputs_begin(args.run, wtrace=args.wtrace, rootmap=args.rootmap)
            print(f"INPUTS: {receipt['mode']}")
            return 0
        problems = inputs_finish(args.run)
        print("VALID" if not problems else "INVALID: " + "; ".join(problems[:10]))
        return 0 if not problems else 2
    if args.cmd == "capture":
        try:
            manifest = capture(args.store, args.out, home=args.home,
                               codex_home=args.codex_home,
                               claude_config_dir=args.claude_config_dir,
                               extras=args.extra, probes=args.probe,
                               retry_deadline_s=args.retry_deadline_s)
        except Refused as exc:
            print(f"REFUSED: {exc}")
            return 2
        seal = manifest["seal"]
        copies = len(manifest["stores"])
        print(f"CAPTURED: {seal['files']} files, {seal['bytes']} bytes, "
              f"{len(manifest['absences'])} recorded absences, bound to "
              f"{copies} store cop{'y' if copies == 1 else 'ies'} -> {args.out} "
              f"(seal {seal_identity(args.out)['sealSha256']})")
        return 0
    if args.cmd == "verify":
        result = verify(args.freeze, full=args.full)
        return _emit(result, args.out, "VERIFIED", "INVALID", result["valid"])
    if args.cmd == "qualify":
        result = qualify(args.store, args.freeze)
        out = args.out or os.path.join(args.store, "frozen-qualification.json")
        return _emit(result, out, "QUALIFIED", "REFUSED", result["qualified"])
    result = family(args.receipts, args.freeze,
                    expect_pids=[int(p) for p in args.expect_pid])
    return _emit(result, args.out, "VALID", "INVALID", result["valid"])


if __name__ == "__main__":
    raise SystemExit(main())
