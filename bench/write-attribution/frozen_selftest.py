"""#901 spec §6.3 "Frozen namespace" (Q16, `dc13` L1): the namespace self-test.

usage: frozen_selftest.py --tree TREE --scratch DIR --rootmap ROOTMAP_DYLIB
                          --wtrace WTRACE_DYLIB [--out JSON] [--red] [--keep]

Runs on THIS host's pinned runtime (Homebrew CPython 3.14, /usr/bin/sandbox-exec)
on synthetic trees under DIR (an external-drive scratch directory): a synthetic
HOME with .codex/sessions/**, .codex/auth.json (multiline JSON),
.codex/config.toml, .codex/state_5.sqlite (a WAL database whose title is
committed only in its WAL: a reader pins it, a PASSIVE checkpoint stays
incomplete, and both connections stay open through the capture),
.claude/projects/** and .claude.json, plus the real roots' two link shapes
(Amendment 10): N_LINKS absolute single-hop links from .codex/sessions to
rollouts under other "repositories'" .codex/sessions outside every root, and a
Claude project's `memory` directory link to a directory outside every root
holding a .jsonl the product walk must never reach. It never reads or
writes the operator's ~/.codex, ~/.claude or the live store. Every process it
starts is bounded and reaped. Exit 0 and a final `frozen-selftest: PASS`
line, or exit 1 naming each failed check.

Sequence:
 1. Measured facts (re-proved, evidence for the design): a plain deny profile
    denies open and stat with EPERM; a subprocess and a forked setsid child
    inherit the denial; sandbox-exec strips DYLD_INSERT_LIBRARIES; the shipped
    profile kills a violating process (SIGKILL).
 2. A native ingest of the synthetic live tree by the tested tree's own
    `cache-sync --source all`, closed into a backup-API source copy; then a
    suffix is appended to the live transcripts (the torn-line rule is pinned
    by the remote tests of frozen_roots.py).
 3. `frozen_roots.py capture` of the live tree against that copy.
 4. CONTROL: a clone of the copy ingests the live tree at its native path
    (no namespace; the live tree still equals the freeze), under wtrace.
 5. The live tree changes: a sentinel record appended, auth.json rewritten
    in place, a new rollout, a Claude append.
 6. FROZEN: another clone ingests through the launch contract (sandbox-exec
    + frozen_launch.py + rootmap.dylib:wtrace.dylib). It must read the frozen
    content with the original identities, store the logical paths, and
    equal the control: every table's rows (wall-clock columns excluded) and
    the interposer's per-store byte classes. Each linked rollout is stored
    under its target's logical canonical path with the target's source
    device and inode, as the control stores it, and nothing under the memory
    link is ingested.
 7. Coverage probe through the launch contract: relative paths (cwd inside
    and above a root), dir_fd calls, a symlink inside a root, descriptor
    reuse and dup, the C ABI variants (bypass helper: open$NOCANCEL,
    openat$NOCANCEL, realpath, realpath$DARWIN_EXTSN, __opendir2, readdir_r,
    fstatat, readlinkat, faccessat), F_GETPATH and getcwd reverse mapping,
    a recorded absence (ENOENT), posix_spawn and fork+exec subprocesses, a
    child whose environment lost DYLD_INSERT_LIBRARIES (re-injected), fork,
    a detached setsid worker, os._exit and a child started through a
    protected system program (/usr/bin/env, admitted and listed with its
    launch record and recorded termination, Q17); through an out-of-root
    file link: lstat shows the link, readlink the logical target, and stat,
    fstat, open, realpath, Path.resolve, F_GETPATH and the C ABI variants
    reach the frozen copy with the target's identity and logical path; the
    memory link is a symlink the product's glob never follows; the family
    verdict must be VALID. In its own launch (Amendment 13 O1), every call
    that FOLLOWS the memory link - os.walk's DirEntry.is_dir, os.stat, an
    fstatat on a directory descriptor, access, os.path.realpath and the C
    realpath variants - and a stat of its target present the target's
    CAPTURED metadata (the live target changed after the capture) with no
    kernel call on it; os.walk classifies the link as a directory and does
    not descend; that family is VALID. Amendment 13 O2: a Codex session
    whose cwd lies in a Codex worktree under the synthetic Codex home is
    captured with probe-only entries; after the capture Codex "removes" the
    worktree, and the tested tree's own git-root project resolution of that
    cwd through the namespace equals its native control at capture time
    (and differs from a native resolution now); that family is VALID.
    Amendment 14 P1: two Claude sessions record the memory link's live
    target and a directory below it as their cwd; the capture records probe
    entries inside the denied target (its `.git`, present; each component
    below it and each one's `.git`, absent), the target's `.git` and the
    directories below it are removed after the capture, and the tested
    tree's own resolution of both cwds through the namespace equals its
    native control; both families are VALID.
 8. Bypasses: the library unloaded (/bin/sh strips it) and a raw
    syscall(SYS_open) from a C helper are killed by the sandbox.
 9. Invalidity: a missing mapping, a write intent, a changed frozen prefix
    (seal), a changed inode (qualification), a denied required read, a
    sandbox kill its parent swallows (Q17), an open of a probe entry
    (`unmapped-open`), a listing of a probe entry inside the denied target
    (`unmapped-open`) and a stat of a file below the target that is no probe
    (`traversal`, Amendment 14), a deliberate traversal of the
    memory link (a listing of the link or of its target, and an open under
    it, refused as a `traversal`; a raw open through the live link and of a
    link's live target killed by the sandbox, the backstop), a lost
    receipt, absent enforcement and a timeout each make the family verdict
    (or the freeze/qualification) INVALID.
--red runs steps 2-5 and the same assertions with the CURRENT runners'
method instead of the namespace (a native launch reading the live roots, a
plain relocated copy, no sandbox): the RED proof that each assertion fails.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import stat as stat_mod
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
PY = "/opt/homebrew/bin/python3"
SANDBOX = "/usr/bin/sandbox-exec"
F_GETPATH = 50
STORES = ("cache.db", "conversations.db", "stats.db")
#: Columns that hold the ingest's own wall clock (never input content); the
#: control and the frozen run ingest at different instants.
WALL_CLOCK = {
    "last_ingested_at", "first_seen_utc", "last_seen_utc", "ingested_at",
    "created_at", "updated_at", "recorded_at", "stamped_at", "observed_at",
    "captured_at_utc", "applied_at", "applied_at_utc", "synced_at",
    "last_sync_at", "last_full_pass_at", "checked_at", "materialized_at",
    "generated_at", "first_ingested_at", "last_checked_at", "completed_at_utc",
    # the control tree (before W8) rewrites this on every walk
    "updated_at_utc",
}
#: (table, column) pairs holding a per-run nonce rather than input content.
NONCE = {("quota_projection_state", "generation")}
#: cache_meta/conversation meta keys holding the ingest's own wall clock.
WALL_CLOCK_KEY_MARKERS = ("_at", "_utc", "time", "stamp", "last_",
                          # its value is the walk's instant; the control tree
                          # (before W8) rewrites it on every complete walk
                          "walk_complete")
BYPASS_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/param.h>
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
extern int open_nc(const char *, int, ...) __asm("_open$NOCANCEL");
extern int openat_nc(int, const char *, int, ...) __asm("_openat$NOCANCEL");
extern char *rp_plain(const char *, char *) __asm("_realpath");
extern char *rp_ext(const char *, char *) __asm("_realpath$DARWIN_EXTSN");
extern DIR *__opendir2(const char *, int);
/* usage: bypass raw PATH | rp PATH | abi FILE DIR NAME LINK */
int main(int argc, char **argv) {
  if (argc == 3 && !strcmp(argv[1], "raw")) {
    long fd = syscall(SYS_open, argv[2], O_RDONLY);
    printf("{\"rawOpen\": %ld, \"errno\": %d}\n", fd, fd < 0 ? errno : 0);
    return 0;
  }
  if (argc == 3 && !strcmp(argv[1], "rp")) {
    char b1[MAXPATHLEN], b2[MAXPATHLEN];
    errno = 0; char *r1 = rp_plain(argv[2], b1); int e1 = r1 ? 0 : errno;
    errno = 0; char *r2 = rp_ext(argv[2], b2); int e2 = r2 ? 0 : errno;
    printf("{\"realpath\": \"%s\", \"realpathExt\": \"%s\", \"errno\": %d, \"errnoExt\": %d}\n",
           r1 ? r1 : "", r2 ? r2 : "", e1, e2);
    return 0;
  }
  if (argc != 6 || strcmp(argv[1], "abi")) return 2;
  const char *file = argv[2], *dir = argv[3], *name = argv[4], *link = argv[5];
  struct stat a, b; char buf[MAXPATHLEN], buf2[MAXPATHLEN], lnk[MAXPATHLEN];
  int f1 = open_nc(file, O_RDONLY); int ok_open_nc = f1 >= 0 && fstat(f1, &a) == 0;
  int dfd = open(dir, O_RDONLY | O_DIRECTORY);
  int f2 = openat_nc(dfd, name, O_RDONLY); int ok_openat_nc = f2 >= 0 && fstat(f2, &b) == 0;
  struct stat c; int ok_fstatat = fstatat(dfd, name, &c, 0) == 0;
  int ok_faccessat = faccessat(dfd, name, R_OK, 0) == 0;
  ssize_t n = readlinkat(AT_FDCWD, link, lnk, sizeof lnk - 1); if (n >= 0) lnk[n] = 0; else lnk[0] = 0;
  char *r1 = rp_plain(file, buf), *r2 = rp_ext(file, buf2);
  DIR *d = __opendir2(dir, 0); unsigned long long dino = 0; int ok_readdir_r = 0;
  if (d) { struct dirent ent, *res = NULL;
    while (readdir_r(d, &ent, &res) == 0 && res) if (!strcmp(res->d_name, name)) { dino = res->d_ino; ok_readdir_r = 1; }
    closedir(d); }
  char gp[MAXPATHLEN]; gp[0] = 0; if (f1 >= 0) fcntl(f1, F_GETPATH, gp);
  printf("{\"openNocancelIno\": %llu, \"openatNocancelIno\": %llu, \"fstatatIno\": %llu,"
         " \"faccessat\": %d, \"readlinkat\": \"%s\", \"realpath\": \"%s\", \"realpathExt\": \"%s\","
         " \"opendir2ReaddirRIno\": %llu, \"readdirR\": %d, \"getpath\": \"%s\", \"okOpen\": %d, \"okOpenat\": %d, \"okFstatat\": %d}\n",
         (unsigned long long)a.st_ino, (unsigned long long)b.st_ino, (unsigned long long)c.st_ino,
         ok_faccessat, lnk, r1 ? r1 : "", r2 ? r2 : "", dino, ok_readdir_r, gp, ok_open_nc, ok_openat_nc, ok_fstatat);
  return 0;
}
"""


class Bounded(Exception):
    pass


def run(argv, *, env=None, timeout=120, cwd=None, check=False, record=None):
    """A bounded child in its own session; its whole group is killed on
    timeout. Returns CompletedProcess (returncode -9 when killed). With
    `record` = (receipts, step) the child is a top-level family process
    (Amendment 13 O3): its end goes to RECEIPTS/toplevel.jsonl, and a timeout
    kill is recorded as the harness's teardown first."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import frozen_roots
    proc = subprocess.Popen(argv, env=env, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            start_new_session=True)

    def ended():
        if record:
            frozen_roots.record_toplevel(record[0], proc.pid, returncode=proc.returncode,
                                         runner="frozen_selftest.py", step=record[1])
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if record:
            frozen_roots.record_teardown(record[0], proc.pid, signal.SIGKILL,
                                         by="frozen_selftest.py", group=True)
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        ended()
        raise Bounded(f"timed out after {timeout}s: {argv[:3]} {err[-500:]}")
    ended()
    result = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{argv[:4]} exited {proc.returncode}: {err[-2000:]}")
    return result


# ── synthetic transcripts ─────────────────────────────────────────────────

def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(at)) + \
        f".{int((at % 1) * 1000):03d}Z"


def codex_lines(thread: str, at: float, turns: int, total: int, tag: str,
                cwd: str = "/bench/frozen-selftest"):
    lines = [{"timestamp": _iso(at), "type": "session_meta", "payload": {
        "id": thread, "session_id": thread, "timestamp": _iso(at),
        "cwd": cwd, "originator": "codex_exec",
        "cli_version": "0.160.0", "source": "exec", "thread_source": "user",
        "model_provider": "openai"}}]
    lines += codex_turns(at + 1, turns, total, tag, cwd)
    return lines


def codex_turns(at: float, turns: int, total: int, tag: str,
                cwd: str = "/bench/frozen-selftest"):
    out = []
    for i in range(turns):
        t = at + 10 * i
        total += 300
        out += [
            {"timestamp": _iso(t), "type": "turn_context", "payload": {
                "cwd": cwd, "model": "gpt-5",
                "turn_id": f"{tag}-turn-{i}"}},
            {"timestamp": _iso(t + 1), "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{
                    "type": "input_text", "text": f"{tag} question {i}"}]}},
            {"timestamp": _iso(t + 2), "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{
                    "type": "output_text", "text": f"{tag} answer {i}"}]}},
            {"timestamp": _iso(t + 3), "type": "event_msg", "payload": {
                "type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 200,
                                         "cached_input_tokens": 25,
                                         "output_tokens": 100,
                                         "reasoning_output_tokens": 25,
                                         "total_tokens": 300},
                    "total_token_usage": {"total_tokens": total}}}},
        ]
    return out


def claude_lines(session: str, at: float, turns: int, tag: str, parent=None,
                 cwd: str = "/bench/frozen-selftest"):
    out = []
    for i in range(turns):
        t = at + 10 * i
        u, a = f"{session}-{tag}-u{i}", f"{session}-{tag}-a{i}"
        out.append({"type": "user", "uuid": u, "parentUuid": parent,
                    "sessionId": session, "timestamp": _iso(t),
                    "cwd": cwd, "gitBranch": "main",
                    "message": {"role": "user",
                                "content": f"{tag} prompt {i}"}})
        out.append({"type": "assistant", "uuid": a, "parentUuid": u,
                    "sessionId": session, "timestamp": _iso(t + 1),
                    "cwd": cwd, "gitBranch": "main",
                    "requestId": f"req-{a}",
                    "message": {"id": f"msg-{a}", "role": "assistant",
                                "model": "claude-sonnet-4-5-20250929",
                                "content": [{"type": "text",
                                             "text": f"{tag} reply {i}"}],
                                "usage": {"input_tokens": 100,
                                          "output_tokens": 50,
                                          "cache_read_input_tokens": 10,
                                          "cache_creation_input_tokens": 5}}})
        parent = a
    return out


def _write_lines(path: pathlib.Path, rows, mode="w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


#: Out-of-root rollout links in the synthetic tree (Amendment 10): more than
#: the real roots' 95, and more than the 64 roots rootmap.c once held.
N_LINKS = 96


class Tree:
    """The synthetic live HOME, plus the out-of-root files and directory its
    links reach (Amendment 10: the real ~/.codex/sessions holds absolute,
    single-hop links to rollouts under other repositories' .codex/sessions,
    and each Claude project may hold a `memory` directory link)."""

    def __init__(self, base: pathlib.Path) -> None:
        self.outside = base / "live" / "outside"
        self.home = base / "live" / "home"
        self.codex = self.home / ".codex"
        self.sessions = self.codex / "sessions" / "2026" / "10" / "05"
        self.claude = self.home / ".claude" / "projects" / "-bench-frozen"
        now = time.time() - 3600
        self.at = now
        self.thread_a = "01a19010-0000-7000-8000-00000000a901"
        self.thread_b = "01a19010-0000-7000-8000-00000000b901"
        self.rollout_a = self.sessions / f"rollout-2026-10-05T09-00-00-{self.thread_a}.jsonl"
        self.rollout_b = self.sessions / f"rollout-2026-10-05T09-05-00-{self.thread_b}.jsonl"
        self.rollout_new = self.sessions / "rollout-2026-10-05T09-30-00-01a19010-0000-7000-8000-00000000c901.jsonl"
        self.session_a = self.claude / "bench-frozen-a.jsonl"
        self.session_b = self.claude / "bench-frozen-b.jsonl"
        self.auth = self.codex / "auth.json"
        self.config = self.codex / "config.toml"
        self.claude_json = self.home / ".claude.json"
        self.link = self.codex / "sessions" / "latest"
        self.history = self.codex / "history.jsonl"     # outside the walk
        self.state = self.codex / "state_5.sqlite"      # a WAL-mode database
        self.hooks = self.codex / "hooks.json"          # recorded absent
        # Amendment 10: absolute links from the Codex walk to rollouts outside
        # every root, and a Claude `memory` directory link the walk never
        # traverses (its .jsonl must never be ingested).
        self.linked_dir = self.codex / "sessions" / "2026" / "10" / "04"
        self.links, self.targets, self.link_threads = [], [], []
        for i in range(N_LINKS):
            thread = f"01a19010-0000-7000-8000-{0xd00000 + i:012x}"
            name = f"rollout-2026-10-04T08-{i // 60:02d}-{i % 60:02d}-{thread}.jsonl"
            self.link_threads.append(thread)
            self.targets.append(self.outside / f"repo-{i % 4}" / ".codex" / "sessions"
                                / "2026" / "10" / "04" / name)
            self.links.append(self.linked_dir / name)
        self.memory = self.claude / "memory"
        self.memory_target = self.outside / "repo-0" / ".agentmem"
        self.memory_file = self.memory_target / "memory-notes.jsonl"
        # Amendment 14 P1: Claude sessions whose working directory IS the
        # memory link's target, and one below it (the real transcripts record
        # both: 5,181 lines name a project's `.claude-memory`); the product's
        # git-root walk-up checks the target's `.git` (present here) and each
        # component's `.git` below it (absent), and every one of them lies in
        # the denied target. Removed after the capture.
        self.memory_git = self.memory_target / ".git"
        self.memory_below = self.memory_target / "notes" / "deep"
        self.session_mem_t = self.claude / "bench-frozen-mem-t.jsonl"
        self.session_mem_d = self.claude / "bench-frozen-mem-d.jsonl"
        # Amendment 13 O2: a Codex session whose working directory lies in a
        # Codex worktree under the Codex home; the product's git-root
        # walk-up lstat's each ancestor and checks each one's `.git`, and
        # Codex removes the worktree later (after the capture, here)
        self.worktree = self.codex / "worktrees" / "9d52" / "cctally-wt"
        self.worktree_cwd = self.worktree / "sub"
        self.thread_w = "01a19010-0000-7000-8000-00000000e901"
        self.rollout_w = self.sessions / f"rollout-2026-10-05T09-10-00-{self.thread_w}.jsonl"

    def build(self) -> None:
        _write_lines(self.rollout_a, codex_lines(self.thread_a, self.at, 3, 0, "A"))
        _write_lines(self.rollout_b, codex_lines(self.thread_b, self.at + 50, 2, 0, "B"))
        _write_lines(self.session_a, claude_lines("bench-frozen-a", self.at, 3, "A"))
        _write_lines(self.session_b, claude_lines("bench-frozen-b", self.at + 50, 2, "B"))
        self.auth.write_text(json.dumps({
            "OPENAI_API_KEY": None, "tokens": {"account_id": "acct-frozen-1",
                                               "id_token": "x.y.z"},
            "last_refresh": "2026-10-05T08:00:00Z"}, indent=2) + "\n")
        self.config.write_text('model = "gpt-5"\nservice_tier = "default"\n')
        self.claude_json.write_text(json.dumps({"oauthAccount": {
            "accountUuid": "00000000-0000-4000-8000-0000000f0901",
            "emailAddress": "frozen-selftest@example.invalid"}}, indent=2) + "\n")
        os.symlink(os.path.relpath(self.rollout_a, self.link.parent), self.link)
        for i, (thread, target, link) in enumerate(zip(self.link_threads, self.targets,
                                                       self.links)):
            _write_lines(target, codex_lines(thread, self.at + 100 + i, 1, 0, f"L{i}"))
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(str(target), link)
        _write_lines(self.memory_file, claude_lines("bench-frozen-memory", self.at, 1,
                                                    "MEMORY-SENTINEL"))
        self.memory_git.write_text("gitdir: /bench/frozen-selftest/.git/worktrees/mem\n")
        self.memory_below.mkdir(parents=True)
        _write_lines(self.session_mem_t, claude_lines(
            "bench-frozen-mem-t", self.at + 60, 1, "MT", cwd=str(self.memory_target)))
        _write_lines(self.session_mem_d, claude_lines(
            "bench-frozen-mem-d", self.at + 65, 1, "MD", cwd=str(self.memory_below)))
        os.symlink(str(self.memory_target), self.memory)
        _write_lines(self.history, [{"text": "outside the walk"}])
        self.worktree_cwd.mkdir(parents=True)
        (self.worktree / ".git").write_text("gitdir: /bench/frozen-selftest/.git/worktrees/9d52\n")
        _write_lines(self.rollout_w, codex_lines(self.thread_w, self.at + 70, 2, 0, "W",
                                                 cwd=str(self.worktree_cwd)))
        # Codex's state database: the title the freeze must keep is committed
        # only in the WAL - a reader pins it, so a PASSIVE checkpoint stays
        # incomplete - and both connections stay open through the capture
        # (spec §6.3 "Frozen input set", Q17, `dc14` N1).
        self.db = sqlite3.connect(self.state, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
        self.db.execute("INSERT INTO threads VALUES (?, 'Checkpointed title')",
                        (self.thread_a,))
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.reader = sqlite3.connect(self.state, isolation_level=None)
        self.reader.execute("BEGIN")
        self.reader.execute("SELECT count(*) FROM threads").fetchone()
        self.db.execute("UPDATE threads SET title = 'Frozen title' WHERE id = ?",
                        (self.thread_a,))
        self.passive = list(self.db.execute(
            "PRAGMA wal_checkpoint(PASSIVE)").fetchone())

    def close_state(self) -> None:
        for name in ("reader", "db"):
            conn = getattr(self, name, None)
            if conn is not None:
                try:
                    if name == "reader" and conn.in_transaction:
                        conn.execute("COMMIT")
                    conn.close()
                except sqlite3.Error:
                    pass
                setattr(self, name, None)

    def grow_before_capture(self) -> None:
        """Suffix beyond the store's cursors."""
        _write_lines(self.rollout_a, codex_turns(self.at + 400, 1, 900, "A2"), "a")
        _write_lines(self.session_b, claude_lines("bench-frozen-b", self.at + 400, 1,
                                                  "B2", "bench-frozen-b-B-a1"), "a")
        _write_lines(self.targets[0], codex_turns(self.at + 400, 1, 900, "L0-2"), "a")

    def mutate_after_capture(self) -> None:
        _write_lines(self.targets[1], codex_turns(self.at + 900, 1, 900, "LIVE-SENTINEL"),
                     "a")
        _write_lines(self.memory_file, claude_lines("bench-frozen-memory", self.at + 900,
                                                    1, "LIVE-SENTINEL"), "a")
        # a new entry changes the live target directory's own mtime: a stat
        # through the link must still present the CAPTURED metadata (O1)
        (self.memory_target / "after-capture.md").write_text("LIVE-SENTINEL\n")
        # P1: the git-root walk-up below the target must read the frozen answers
        self.memory_git.unlink()
        shutil.rmtree(self.memory_below.parent)
        # Codex removes the worktree: the walk-up must read the frozen answers (O2)
        shutil.rmtree(self.worktree.parent)
        _write_lines(self.rollout_b, codex_turns(self.at + 900, 1, 900, "LIVE-SENTINEL"), "a")
        _write_lines(self.rollout_a, [{"timestamp": _iso(self.at + 900),
                                       "type": "response_item", "payload": {
                                           "type": "message", "role": "user",
                                           "content": [{"type": "input_text",
                                                        "text": "LIVE-SENTINEL"}]}}], "a")
        _write_lines(self.rollout_new, codex_lines(
            "01a19010-0000-7000-8000-00000000c901", self.at + 950, 1, 0, "NEW"))
        _write_lines(self.session_a, claude_lines("bench-frozen-a", self.at + 900, 1,
                                                  "LIVE-SENTINEL", "bench-frozen-a-A-a2"), "a")
        with open(self.auth, "r+") as fh:        # rewritten in place
            fh.write('{"OPENAI_API_KEY": "LIVE-SENTINEL"')
        self.reader.execute("COMMIT")            # the WAL is no longer pinned
        self.reader.close()
        self.reader = None
        self.db.execute("UPDATE threads SET title = 'LIVE-SENTINEL'")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# ── stores ────────────────────────────────────────────────────────────────

def env_for(tree: Tree, data: pathlib.Path, *, home=None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DYLD_", "WTRACE_", "ROOTMAP_", "CODEX_HOME",
                                "CLAUDE_CONFIG_DIR", "CCTALLY_"))}
    env.update({"HOME": str(home or tree.home), "CCTALLY_DATA_DIR": str(data),
                "PYTHONDONTWRITEBYTECODE": "1",
                "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
                "CCTALLY_DISABLE_TELEMETRY": "1"})
    return env


def backup_copy(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.mkdir(parents=True)
    for name in STORES:
        if not (src / name).exists():
            continue
        a = sqlite3.connect(f"file:{src / name}?mode=ro", uri=True)
        b = sqlite3.connect(dst / name)
        try:
            a.backup(b)
            b.execute("PRAGMA journal_mode=DELETE").fetchone()
        finally:
            a.close()
            b.close()
    if (src / "config.json").exists():
        shutil.copy2(src / "config.json", dst / "config.json")


def clone(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    run(["cp", "-cR", str(src), str(dst)], timeout=60, check=True)


def table_rows(data: pathlib.Path) -> dict:
    out = {}
    for name in STORES:
        db = data / name
        if not db.exists():
            continue
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT "
                "LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL%'")]
            for table in sorted(tables):
                try:
                    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                    keep = [c for c in cols if c not in WALL_CLOCK
                            and (table, c) not in NONCE]
                    rows = conn.execute(
                        f'SELECT {", ".join(chr(34) + c + chr(34) for c in keep)} '
                        f'FROM "{table}"').fetchall()
                except sqlite3.Error:
                    continue
                if "key" in keep and "value" in keep:
                    ki = keep.index("key")
                    rows = [r for r in rows if not any(
                        m in str(r[ki]) for m in WALL_CLOCK_KEY_MARKERS)]
                out[f"{name}:{table}"] = sorted(repr(r) for r in rows)
        finally:
            conn.close()
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def settle(prefix: pathlib.Path, timeout: float = 30.0) -> list:
    """Wait (bounded) until every process traced under `prefix` has ended:
    the ingest returns while its detached artifact-retention worker may still
    write (`logs/artifact-retention.log`, 67 bytes, observed missing from one
    frozen run's snapshot set), and only an ended process's `.exit` snapshot
    counts. Returns the pids still running at the bound."""
    deadline = time.monotonic() + timeout
    while True:
        pending = []
        for p in prefix.parent.glob(prefix.name + ".*"):
            rest = p.name[len(prefix.name) + 1:]
            if rest.isdigit() and not (prefix.parent / f"{p.name}.exit").exists() \
                    and _alive(int(rest)):
                pending.append(int(rest))
        if not pending or time.monotonic() >= deadline:
            return pending
        time.sleep(0.1)


def byte_classes(prefix: pathlib.Path) -> dict:
    """Per-store byte classes from every exit snapshot under `prefix`."""
    out = {}
    for path in sorted(prefix.parent.glob(prefix.name + ".*.exit")):
        lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        if not lines:
            continue
        for name, (written, _calls) in lines[-1]["paths"].items():
            base = os.path.basename(name)
            key = ("temp" if base.startswith("etilqs_") else
                   base if base.split("-")[0] in STORES else "other")
            out[key] = out.get(key, 0) + written
    return out


# ── the probe (runs INSIDE the launch) ────────────────────────────────────

def rootmap_active() -> bool:
    try:
        return bool(ctypes.CDLL(None).rootmap_active())
    except AttributeError:
        return False


def _ident(st) -> list:
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def _getpath(fd) -> str:
    return fcntl.fcntl(fd, F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0].decode()


def child_checks(plan: dict) -> dict:
    t = plan["transcript"]
    with open(t["logical"], "rb") as fh:
        data = fh.read()
    st = os.stat(t["logical"])
    return {"active": rootmap_active(),
            "frozenContent": hashlib.sha256(data).hexdigest() == t["sha256"],
            "identity": _ident(st) == t["identity"]}


def probe(plan: dict) -> dict:
    res = {}
    t = plan["transcript"]
    res["active"] = rootmap_active()
    with open(t["logical"], "rb") as fh:
        data = fh.read()
        res["fstatIdentity"] = _ident(os.fstat(fh.fileno())) == t["identity"]
        res["getpathLogical"] = _getpath(fh.fileno()) == t["logical"]
    res["frozenContent"] = (hashlib.sha256(data).hexdigest() == t["sha256"]
                            and b"LIVE-SENTINEL" not in data)
    res["statIdentity"] = _ident(os.stat(t["logical"])) == t["identity"]
    with os.scandir(plan["sessionsDir"]) as it:
        names = {e.name: e.inode() for e in it}
    res["scandirInode"] = names.get(os.path.basename(t["logical"])) == t["identity"][1]
    res["listingFrozen"] = sorted(names) == sorted(plan["frozenNames"])
    # relative paths: cwd above a root, and inside one
    os.chdir(plan["home"])
    rel = os.path.relpath(t["logical"], plan["home"])
    res["relativeAbove"] = _ident(os.stat(rel)) == t["identity"]
    os.chdir(plan["codexRoot"])
    res["getcwdLogical"] = os.getcwd() == plan["codexRoot"]
    with open(os.path.relpath(t["logical"], plan["codexRoot"]), "rb") as fh:
        res["relativeInside"] = hashlib.sha256(fh.read()).hexdigest() == t["sha256"]
    os.chdir(plan["scratch"])
    # dir_fd calls
    dfd = os.open(plan["sessionsDir"], os.O_RDONLY)
    name = os.path.basename(t["logical"])
    res["dirfdStat"] = _ident(os.stat(name, dir_fd=dfd)) == t["identity"]
    fd = os.open(name, os.O_RDONLY, dir_fd=dfd)
    res["dirfdOpen"] = _ident(os.fstat(fd)) == t["identity"]
    res["dirfdAccess"] = os.access(name, os.R_OK, dir_fd=dfd)
    res["dirfdGetpath"] = _getpath(dfd) == plan["sessionsDir"]
    with os.scandir(dfd) as it:
        res["fdopendirInode"] = any(e.name == name and e.inode() == t["identity"][1]
                                    for e in it)
    # descriptor reuse and dup
    dup = os.dup(fd)
    res["dupIdentity"] = _ident(os.fstat(dup)) == t["identity"]
    os.close(dup)
    num = fd
    os.close(fd)
    other = os.open(os.path.join(plan["scratch"], "reuse.bin"),
                    os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    real = os.stat(os.path.join(plan["scratch"], "reuse.bin"))
    res["reuseUnpatched"] = (_ident(os.fstat(other))[:2] == [real.st_dev, real.st_ino]
                             and real.st_ino != t["identity"][1])
    res["reuseSameNumber"] = other == num
    os.close(other)
    os.close(dfd)
    # symlink inside a root
    link = plan["link"]
    res["readlinkStored"] = os.readlink(link) == plan["linkTarget"]
    res["lstatIsLink"] = stat_mod.S_ISLNK(os.lstat(link).st_mode)
    res["statThroughLink"] = os.stat(link).st_ino == t["identity"][1]
    res["realpathLogical"] = os.path.realpath(link) == t["logical"]
    # metadata captured whole, and a recorded absence
    with open(plan["auth"]["logical"], "rb") as fh:
        res["authFrozen"] = hashlib.sha256(fh.read()).hexdigest() == plan["auth"]["sha256"]
    # a WAL-mode database read the way the dashboard reads it (mode=ro)
    state = pathlib.Path(plan["state"])
    try:
        conn = sqlite3.connect(f"{state.resolve().as_uri()}?mode=ro", uri=True,
                               timeout=0.05)
        titles = [list(r) for r in conn.execute(
            "SELECT id, title FROM threads WHERE id IN (?)", (plan["stateThread"],))]
        conn.close()
    except sqlite3.Error as exc:
        titles = [repr(exc)]
    res["stateDatabaseFrozen"] = titles == [[plan["stateThread"], "Frozen title"]]
    sidecars = sorted(n for n in os.listdir(plan["stateFrozenDir"])
                      if n.startswith("state_5.sqlite-"))
    res["stateNoSidecar"] = not sidecars
    try:
        os.stat(plan["absent"])
        res["absentEnoent"] = False
    except FileNotFoundError:
        res["absentEnoent"] = True
    except OSError:
        res["absentEnoent"] = False
    # the C ABI variants
    out = subprocess.run([plan["bypass"], "abi", t["logical"], plan["sessionsDir"],
                          name, link], capture_output=True, text=True, timeout=30)
    abi = json.loads(out.stdout) if out.returncode == 0 else {}
    ino = t["identity"][1]
    res["abiOpenNocancel"] = abi.get("openNocancelIno") == ino
    res["abiOpenatNocancel"] = abi.get("openatNocancelIno") == ino
    res["abiFstatat"] = abi.get("fstatatIno") == ino
    res["abiFaccessat"] = abi.get("faccessat") == 1
    res["abiReadlinkat"] = abi.get("readlinkat") == plan["linkTarget"]
    res["abiRealpath"] = abi.get("realpath") == t["logical"]
    res["abiRealpathExt"] = abi.get("realpathExt") == t["logical"]
    res["abiOpendir2ReaddirR"] = abi.get("opendir2ReaddirRIno") == ino
    res["abiGetpath"] = abi.get("getpath") == t["logical"]
    # Amendment 10: a link to a file outside every root presents a symlink
    # whose readlink is the logical target; everything through it reaches the
    # frozen copy with the target's source identity and its logical path
    fl = plan["fileLink"]
    res["fileLinkLstatIsLink"] = stat_mod.S_ISLNK(os.lstat(fl["logical"]).st_mode) \
        and os.lstat(fl["logical"]).st_ino == fl["linkIno"]
    res["fileLinkReadlinkLogicalTarget"] = os.readlink(fl["logical"]) == fl["target"]
    res["fileLinkStatTargetIdentity"] = _ident(os.stat(fl["logical"])) == fl["identity"]
    with open(fl["logical"], "rb") as fh:
        res["fileLinkFrozenContent"] = (hashlib.sha256(fh.read()).hexdigest()
                                        == fl["sha256"])
        res["fileLinkFstatIdentity"] = _ident(os.fstat(fh.fileno())) == fl["identity"]
        res["fileLinkGetpathTarget"] = _getpath(fh.fileno()) == fl["target"]
    res["fileLinkRealpathTarget"] = os.path.realpath(fl["logical"]) == fl["target"]
    res["fileLinkResolveTarget"] = str(pathlib.Path(fl["logical"]).resolve()) == fl["target"]
    res["fileLinkTargetPathIdentity"] = _ident(os.stat(fl["target"])) == fl["identity"]
    out = subprocess.run([plan["bypass"], "abi", fl["logical"], fl["dir"], fl["name"],
                          fl["logical"]], capture_output=True, text=True, timeout=30)
    abi = json.loads(out.stdout) if out.returncode == 0 else {}
    ino = fl["identity"][1]
    res["fileLinkAbiOpenNocancel"] = abi.get("openNocancelIno") == ino
    res["fileLinkAbiOpenatNocancel"] = abi.get("openatNocancelIno") == ino
    res["fileLinkAbiFstatat"] = abi.get("fstatatIno") == ino
    res["fileLinkAbiReadlinkat"] = abi.get("readlinkat") == fl["target"]
    res["fileLinkAbiRealpath"] = abi.get("realpath") == fl["target"]
    res["fileLinkAbiRealpathExt"] = abi.get("realpathExt") == fl["target"]
    res["fileLinkAbiDirentIsTheLink"] = abi.get("opendir2ReaddirRIno") == fl["linkIno"]
    res["fileLinkAbiGetpath"] = abi.get("getpath") == fl["target"]
    # the memory directory link: a symlink the walk never follows
    dl = plan["dirLink"]
    res["dirLinkLstatIsLink"] = stat_mod.S_ISLNK(os.lstat(dl["logical"]).st_mode)
    res["dirLinkReadlinkLogicalTarget"] = os.readlink(dl["logical"]) == dl["target"]
    with os.scandir(os.path.dirname(dl["logical"])) as it:
        ent = next((e for e in it if e.name == os.path.basename(dl["logical"])), None)
    res["dirLinkScandirIsLink"] = (ent is not None and ent.is_symlink()
                                   and not ent.is_dir(follow_symlinks=False))
    walked = sorted(str(p) for p in pathlib.Path(plan["claudeProjects"]).glob("**/*.jsonl"))
    res["dirLinkNotGlobbed"] = bool(walked) and not any(
        p.startswith(dl["logical"] + "/") for p in walked)
    # subprocesses, a stripped environment, fork, setsid detach, os._exit
    results = pathlib.Path(plan["results"])
    me = [sys.executable, os.path.abspath(__file__), "child", "--plan", plan["planPath"]]
    spawned = subprocess.run(me + ["--out", str(results / "spawn.json")], timeout=60)
    res["spawnChild"] = spawned.returncode == 0 and _all(results / "spawn.json")
    stripped = {k: v for k, v in os.environ.items() if k != "DYLD_INSERT_LIBRARIES"}
    s2 = subprocess.run(me + ["--out", str(results / "stripped.json")], env=stripped,
                        timeout=60)
    res["strippedChildReinjected"] = s2.returncode == 0 and _all(results / "stripped.json")
    s3 = subprocess.run(me + ["--out", str(results / "forkexec.json")],
                        preexec_fn=lambda: None, timeout=60)
    res["forkExecChild"] = s3.returncode == 0 and _all(results / "forkexec.json")
    # a child started through a protected system program (Q17): no library
    # loads into it; its launch and termination are recorded by this parent
    env_child = subprocess.run(["/usr/bin/env", "/usr/bin/true"], timeout=30)
    res["systemProgramChild"] = env_child.returncode == 0
    pid = os.fork()
    if pid == 0:
        try:
            _dump(results / "fork.json", child_checks(plan))
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    res["forkChild"] = _all(results / "fork.json")
    pid = os.fork()
    if pid == 0:                      # a detached worker: setsid + double fork
        try:
            os.setsid()
            if os.fork() == 0:
                try:
                    _dump(results / "detached.json", child_checks(plan))
                finally:
                    os._exit(0)
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not (results / "detached.json").exists():
        time.sleep(0.05)
    res["detachedSetsidWorker"] = _all(results / "detached.json")
    return res


def dirlink_checks(plan: dict) -> dict:
    """Amendment 13 O1, inside the launch: the calls that FOLLOW a
    non-traversed directory link (os.walk's DirEntry.is_dir, os.stat, an
    fstatat relative to a directory descriptor, access, realpath) and stat of
    its target are answered from the captured target metadata with no kernel
    call on the live target (the sandbox kills one), and os.walk classifies
    the link as a directory without descending into it."""
    dl = plan["dirLink"]
    res = {"active": rootmap_active()}
    dirs, files = {}, []
    for dirpath, dirnames, filenames in os.walk(plan["claudeProjects"]):
        dirs[dirpath] = sorted(dirnames)
        files += [os.path.join(dirpath, n) for n in filenames]
    parent, name = os.path.split(dl["logical"])
    res["walkClassifiesLinkAsDir"] = name in dirs.get(parent, [])
    res["walkDoesNotDescend"] = dl["logical"] not in dirs and not any(
        p.startswith(dl["logical"] + "/") for p in files)
    res["walkReturnsTranscripts"] = sorted(
        p for p in files if p.endswith(".jsonl")) == sorted(plan["claudeTranscripts"])

    def ident(st):
        return [st.st_dev, st.st_ino, st.st_mode, st.st_mtime_ns]
    res["statThroughLinkCaptured"] = ident(os.stat(dl["logical"])) == dl["identity"]
    res["statTargetCaptured"] = ident(os.stat(dl["target"])) == dl["identity"]
    res["lstatTargetCaptured"] = ident(os.lstat(dl["target"])) == dl["identity"]
    dfd = os.open(parent, os.O_RDONLY)
    try:
        res["fstatatThroughLinkCaptured"] = ident(os.stat(name, dir_fd=dfd)) == dl["identity"]
    finally:
        os.close(dfd)
    res["isdirThroughLink"] = os.path.isdir(dl["logical"])
    res["accessThroughLink"] = os.access(dl["logical"], os.R_OK)
    res["lstatStillLink"] = stat_mod.S_ISLNK(os.lstat(dl["logical"]).st_mode)
    res["realpathThroughLink"] = os.path.realpath(dl["logical"]) == dl["target"]
    out = subprocess.run([plan["bypass"], "rp", dl["logical"]], capture_output=True,
                         text=True, timeout=30)
    rp = json.loads(out.stdout) if out.returncode == 0 else {}
    res["cRealpathThroughLink"] = rp.get("realpath") == dl["target"]
    res["cRealpathExtThroughLink"] = rp.get("realpathExt") == dl["target"]
    return res


def project_resolution(tree: str, cwd: str) -> dict:
    """The tested tree's own git-root project resolution for `cwd` (Amendment
    13 O2): `os.path.realpath` (an lstat per component) and the walk-up that
    checks each ancestor's `.git`; its result reaches stored rows."""
    sys.path.insert(0, str(pathlib.Path(tree) / "bin"))
    import _cctally_cache
    key = _cctally_cache._resolve_project_key(cwd, "git-root", {})
    return {"bucket": key.bucket_path, "display": key.display_key,
            "gitRoot": key.git_root, "noGit": key.is_no_git}


def _dump(path, obj) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _all(path) -> bool:
    try:
        return all(json.loads(pathlib.Path(path).read_text()).values())
    except (OSError, ValueError):
        return False


# ── the self-test ─────────────────────────────────────────────────────────

class SelfTest:
    def __init__(self, args) -> None:
        self.args = args
        self.tree_dir = pathlib.Path(args.tree).resolve()
        self.base = pathlib.Path(args.scratch).resolve() / f"frozen-selftest-{os.getpid()}"
        self.base.mkdir(parents=True)
        self.tree = Tree(self.base)
        self.checks = []
        self.facts = {}
        self.rootmap = os.path.realpath(args.rootmap)
        self.wtrace = os.path.realpath(args.wtrace)
        self.cctally = str(self.tree_dir / "bin" / "cctally")

    def check(self, name: str, ok: bool, detail="") -> None:
        self.checks.append({"check": name, "ok": bool(ok),
                            "detail": "" if ok else str(detail)[:1500]})

    # -- launch contracts -------------------------------------------------
    def launch(self, argv, env, receipts: pathlib.Path, *, freeze=None,
               libs="", timeout=180, profile=None):
        freeze = freeze or self.freeze
        step = " ".join(os.path.basename(str(a)) for a in argv[:3])
        return run([SANDBOX, "-f", str(profile or (freeze / "frozen.sb")), PY,
                    str(HERE / "frozen_launch.py"), "--freeze", str(freeze),
                    "--rootmap", self.rootmap, "--receipts", str(receipts),
                    "--libs", libs, "--"] + argv, env=env, timeout=timeout,
                   record=(receipts, step))

    def ingest(self, data, env, *, prefix, frozen: bool, receipts=None):
        env = dict(env, WTRACE_OUT=str(prefix), WTRACE_PERIOD="5")
        argv = [PY, self.cctally, "cache-sync", "--source", "all"]
        if frozen:
            return self.launch(argv, env, receipts, libs=self.wtrace)
        env["DYLD_INSERT_LIBRARIES"] = self.wtrace
        return run(argv, env=env, timeout=180)

    # -- 1. measured facts --------------------------------------------------
    def measured_facts(self) -> None:
        d = self.base / "facts"
        live = d / "live"
        live.mkdir(parents=True)
        (live / "s.txt").write_text("secret\n")
        plain = d / "plain.sb"
        plain.write_text(f'(version 1)(allow default)(deny file-read* file-write* '
                         f'(subpath "{live}"))\n')
        code = ("import os,subprocess,sys,json\n"
                f"p={str(live / 's.txt')!r}\n"
                "def tr(f):\n"
                " try: f(); return 'ok'\n"
                " except OSError as e: return e.errno\n"
                "r={'open':tr(lambda: open(p).read()),'stat':tr(lambda: os.stat(p)),"
                "'dyld':os.environ.get('DYLD_INSERT_LIBRARIES')}\n"
                "c=subprocess.run([sys.executable,'-c',"
                "'import os\\ntry: os.stat(%r); print(0)\\nexcept OSError as e: print(e.errno)'%p],"
                "capture_output=True,text=True)\n"
                "r['subprocess']=int(c.stdout.strip() or -1)\n"
                "rd,wr=os.pipe()\n"
                "if os.fork()==0:\n"
                " os.setsid()\n"
                " try: os.stat(p); v=0\n"
                " except OSError as e: v=e.errno\n"
                " os.write(wr,str(v).encode()); os._exit(0)\n"
                "os.close(wr); r['setsid']=int(os.read(rd,10) or -1)\n"
                "print(json.dumps(r))\n")
        env = dict(os.environ, DYLD_INSERT_LIBRARIES=self.wtrace)
        out = run([SANDBOX, "-f", str(plain), PY, "-c", code], env=env, timeout=60)
        facts = json.loads(out.stdout or "{}")
        self.facts["plainDeny"] = facts
        self.check("fact.openDeniedEperm", facts.get("open") == 1, facts)
        self.check("fact.statDeniedEperm", facts.get("stat") == 1, facts)
        self.check("fact.subprocessInheritsDenial", facts.get("subprocess") == 1, facts)
        self.check("fact.setsidChildInheritsDenial", facts.get("setsid") == 1, facts)
        self.check("fact.sandboxExecStripsDyld", facts.get("dyld") is None, facts)
        killer = d / "kill.sb"
        killer.write_text(f'(version 1)(allow default)(deny file-read* file-write* '
                          f'(subpath "{live}") (with send-signal SIGKILL))\n')
        out = run([SANDBOX, "-f", str(killer), PY, "-c",
                   f"import os; os.stat({str(live / 's.txt')!r}); print('read')"],
                  timeout=60)
        self.facts["killProfile"] = {"returncode": out.returncode, "stdout": out.stdout}
        self.check("fact.violationKilled", out.returncode == -9 and "read" not in out.stdout,
                   self.facts["killProfile"])

    # -- 2-6. store, capture, control, mutation, frozen run --------------------
    def prepare(self) -> None:
        t = self.tree
        t.build()
        native = self.base / "native-data"
        out = run([PY, self.cctally, "cache-sync", "--source", "all"],
                  env=env_for(t, native), timeout=180)
        self.check("setup.nativeIngest", out.returncode == 0, out.stderr[-1500:])
        self.src = self.base / "src"
        backup_copy(native, self.src / "data")
        time.sleep(0.05)
        t.grow_before_capture()

    def capture(self) -> None:
        t = self.tree
        self.freeze = self.base / "freeze"
        out = run([PY, str(HERE / "frozen_roots.py"), "capture", "--store", str(self.src),
                   "--out", str(self.freeze), "--home", str(t.home),
                   "--extra", str(t.link), "--extra", str(t.hooks)],
                  env=env_for(t, self.base / "unused"), timeout=120)
        self.check("capture", out.returncode == 0, out.stdout + out.stderr)
        self.manifest = json.loads((self.freeze / "manifest.json").read_text())
        self.entries = {e["logical"]: e for e in self.manifest["entries"]}
        busy, log, done = t.passive
        self.facts["statePassiveCheckpoint"] = t.passive
        self.check("fact.stateWalOnlyTitle", busy == 0 and log > 0 and done < log,
                   f"the PASSIVE checkpoint completed ({t.passive}): the title "
                   "is not committed only in the WAL")
        # Amendment 10: every out-of-root rollout link covered (its target a
        # mapped single-file root with its own source identity), the memory
        # directory link recorded as non-traversed, nothing under it captured
        links = [self.entries.get(str(p)) or {} for p in t.links]
        target_roots = {r["logical"]: r for r in self.manifest["roots"]
                        if r.get("origin") == "link-target"}
        self.check("capture.outsideLinksCovered", all(
            e.get("kind") == "symlink" and e.get("targetScope") == "outside-roots"
            and e.get("resolved") == str(tg) and str(tg) in target_roots
            and (self.entries.get(str(tg)) or {}).get("ino") == os.stat(tg).st_ino
            for e, tg in zip(links, t.targets)) and len(target_roots) == N_LINKS,
                   {"links": links[:2], "targetRoots": len(target_roots)})
        memory = self.entries.get(str(t.memory)) or {}
        # nothing under the link or its target is captured (Amendment 14's
        # probe entries in the target are metadata only, never a copy)
        self.check("capture.dirLinkNotTraversed", memory.get("class") == "dir-link"
                   and memory.get("traversed") is False
                   and memory.get("resolved") == str(t.memory_target)
                   and not any(k.startswith((str(t.memory) + "/", str(t.memory_target)))
                               and (e.get("class") != "probe" or "physical" in e)
                               for k, e in self.entries.items())
                   and [d["logical"] for d in self.manifest.get("deniedTargets") or []]
                   == [str(t.memory_target)], memory)
        # O1: the target directory's own lstat, never its contents
        meta = next((d.get("metadata") for d in self.manifest.get("deniedTargets") or []), None)
        live = os.lstat(t.memory_target)
        self.check("capture.dirLinkTargetMetadata", isinstance(meta, dict)
                   and meta.get("type") == "dir"
                   and [meta.get(k) for k in ("dev", "ino", "mode", "mtimeNs")]
                   == [live.st_dev, live.st_ino, live.st_mode, live.st_mtime_ns], meta)
        state = self.entries.get(str(t.state)) or {}
        self.check("capture.stateDerived", state.get("class") == "sqlite-derived"
                   and (state.get("source") or {}).get("wal", {}).get("present") is True
                   and (state.get("derived") or {}).get("recovery", {})
                   .get("integrityCheck") == "ok", state)
        # O2: probe-only entries for the worktree session's walk-up, and the
        # product's resolution natively at capture time (the control)
        probes = {e["logical"]: e for e in self.manifest["entries"]
                  if e.get("class") == "probe"}
        absences = {a["logical"] for a in self.manifest["absences"]}
        wt = t.worktree
        self.check("capture.worktreeProbes", all(str(p) in probes for p in (
            wt.parent.parent, wt.parent, wt, t.worktree_cwd, wt / ".git"))
                   and str(t.worktree_cwd / ".git") in absences
                   and str(t.codex / ".git") in absences
                   and not any("physical" in e for e in probes.values()),
                   {"probes": sorted(probes), "summary": self.manifest.get("probes")})
        out = run([PY, __file__, "project", "--tree", str(self.tree_dir), "--cwd",
                   str(t.worktree_cwd)], env=env_for(t, self.base / "unused"), timeout=60)
        self.native_project = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
        self.check("control.projectResolvesGitRoot",
                   self.native_project.get("gitRoot") == str(wt), (out.stdout, out.stderr[-500:]))
        # Amendment 14 P1: probe entries at and below the memory link's
        # denied target, for the cwds equal to it and below it: the target's
        # own `.git` (present), each component below it and each one's `.git`
        # (absent); the target itself keeps O1's presentation (no entry)
        target = str(t.memory_target)
        below = {k: e for k, e in probes.items() if k.startswith(target + "/")}
        mem_t = t.memory_target
        self.check("capture.targetProbes",
                   sorted(below) == sorted(str(p) for p in (
                       t.memory_git, mem_t / "notes", t.memory_below))
                   and (below.get(str(t.memory_git)) or {}).get("type") == "file"
                   and all((below.get(str(p)) or {}).get("type") == "dir"
                           for p in (mem_t / "notes", t.memory_below))
                   and all(e.get("deniedTarget") == target and "physical" not in e
                           for e in below.values())
                   and {str(mem_t / "notes" / ".git"), str(t.memory_below / ".git")}
                   <= absences
                   and target not in probes and target not in absences
                   and (self.manifest.get("probes") or {}).get("targetCwds") == 2,
                   {"below": sorted(below), "summary": self.manifest.get("probes")})
        self.native_target_projects = {}
        for name, cwd in (("memoryTarget", t.memory_target),
                          ("belowMemoryTarget", t.memory_below)):
            out = run([PY, __file__, "project", "--tree", str(self.tree_dir), "--cwd",
                       str(cwd)], env=env_for(t, self.base / "unused"), timeout=60)
            got = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
            self.native_target_projects[name] = (str(cwd), got)
            self.check(f"control.{name}ResolvesGitRoot", got.get("gitRoot") == target,
                       (out.stdout, out.stderr[-500:]))
        out = run([PY, str(HERE / "frozen_roots.py"), "verify", "--freeze",
                   str(self.freeze), "--full"], timeout=60)
        self.check("verify.afterCapture", out.returncode == 0, out.stdout)
        out = run([PY, str(HERE / "frozen_roots.py"), "qualify", "--store", str(self.src),
                   "--freeze", str(self.freeze), "--out", str(self.base / "qualify.json")],
                  timeout=60)
        self.check("qualify", out.returncode == 0, out.stdout)

    def control(self) -> None:
        data = self.base / "control" / "data"
        clone(self.src / "data", data)
        (self.base / "control" / "trace").mkdir()
        out = self.ingest(data, env_for(self.tree, data),
                          prefix=self.base / "control" / "trace" / "w", frozen=False)
        self.check("control.ingest", out.returncode == 0, out.stderr[-1500:])
        late = settle(self.base / "control" / "trace" / "w")
        self.check("control.familyEnded", not late, f"still running: {late}")
        self.control_rows = table_rows(data)
        self.control_bytes = byte_classes(self.base / "control" / "trace" / "w")

    def frozen_run(self, red: bool) -> None:
        t = self.tree
        data = self.base / "frozen" / "data"
        clone(self.src / "data", data)
        (self.base / "frozen" / "trace").mkdir()
        self.receipts = self.base / "frozen" / "receipts"
        prefix = self.base / "frozen" / "trace" / "w"
        if red:   # the current runners: a native launch reading the live roots
            out = self.ingest(data, env_for(t, data), prefix=prefix, frozen=False)
        else:
            out = self.ingest(data, env_for(t, data), prefix=prefix, frozen=True,
                              receipts=self.receipts)
        self.check("frozen.ingest", out.returncode == 0, out.stderr[-1500:])
        late = settle(prefix)
        self.check("frozen.familyEnded", not late, f"still running: {late}")
        rows = table_rows(data)
        diff = sorted(k for k in set(rows) | set(self.control_rows)
                      if rows.get(k) != self.control_rows.get(k))
        self.check("frozen.rowsEqualControl", not diff,
                   {k: {"frozen": (rows.get(k) or [])[:3],
                        "control": (self.control_rows.get(k) or [])[:3]}
                    for k in diff[:3]})
        blob = json.dumps(rows)
        self.check("frozen.noLiveSentinel", "LIVE-SENTINEL" not in blob,
                   "the run ingested the live tree's post-capture change")
        self.check("frozen.storesLogicalPaths", str(t.rollout_a) in blob
                   and str(self.freeze) not in blob, "stored paths are not logical")
        # Amendment 10: each linked rollout keeps the walked link path as its
        # cursor row (Codex persists the discovered spelling) with the
        # target's source device and inode, and its durable file identity is
        # keyed on the target's logical canonical path (`codex_file_key(root,
        # _canonical_codex_path(link))`), as the native-path control stores
        # them; nothing under the memory link
        sys.path.insert(0, str(self.tree_dir / "bin"))
        import _lib_source_identity as source_identity
        linked = {}
        for d in (data, self.base / "control" / "data"):
            conn = sqlite3.connect(f"file:{d / 'cache.db'}?mode=ro", uri=True)
            try:
                rows = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
                    "SELECT path, device_id, inode, source_root_key "
                    "FROM codex_session_files")}
                idents = {r[0] for r in conn.execute(
                    "SELECT file_identity FROM codex_file_accounts")}
            finally:
                conn.close()
            linked[str(d)] = (rows, idents)
        (mine, my_ids), (theirs, their_ids) = (
            linked[str(data)], linked[str(self.base / "control" / "data")])
        got = {}
        for link, tg in zip(t.links, t.targets):
            e = self.entries[str(tg)]
            row = mine.get(str(link))
            key = (source_identity.codex_file_key(row[2], str(tg))
                   if row and row[2] else None)
            got[str(link)] = {
                "row": row, "control": theirs.get(str(link)),
                "identityAtTarget": key in my_ids and key in their_ids,
                "ok": row is not None and row[:2] == (e["dev"], e["ino"])
                and row == theirs.get(str(link)) and key in my_ids
                and key in their_ids}
        self.check("frozen.linkedRolloutsAtTargets",
                   all(g["ok"] for g in got.values()) and len(got) == N_LINKS,
                   {k: v for k, v in list(got.items())[:2]})
        control_blob = json.dumps(self.control_rows)
        # (the target's own path is a recorded cwd since Amendment 14, so the
        # rows name it; no file under the link or the target may be named)
        self.check("frozen.dirLinkNotIngested", "MEMORY-SENTINEL" not in blob
                   and "MEMORY-SENTINEL" not in control_blob
                   and str(t.memory) not in blob and str(t.memory_file) not in blob,
                   "the run ingested a file under the memory directory link")
        got = byte_classes(prefix)
        self.check("frozen.byteClassesEqualControl", got == self.control_bytes,
                   {"frozen": got, "control": self.control_bytes})
        if not red:
            fam = self.family(self.receipts)
            self.check("frozen.familyValid", fam["valid"], fam["problems"])

    def family(self, receipts, freeze=None):
        sys.path.insert(0, str(HERE))
        import frozen_roots
        return frozen_roots.family(receipts, freeze or self.freeze)

    # -- 7. coverage probe ------------------------------------------------------
    def capture_extra(self, name: str) -> pathlib.Path:
        out_dir = self.base / name
        out = run([PY, str(HERE / "frozen_roots.py"), "capture", "--store", str(self.src),
                   "--out", str(out_dir), "--home", str(self.tree.home)],
                  env=env_for(self.tree, self.base / "unused"), timeout=120)
        if out.returncode != 0:
            raise RuntimeError(f"capture {name}: {out.stdout}")
        return out_dir

    def build_bypass(self) -> str:
        src = self.base / "bypass.c"
        src.write_text(BYPASS_C)
        exe = self.base / "bypass"
        out = run(["clang", "-O1", "-o", str(exe), str(src)], timeout=120)
        if out.returncode != 0:
            raise RuntimeError(f"bypass helper build failed: {out.stderr}")
        return str(exe)

    def plan(self, *, home_override=None) -> dict:
        t = self.tree
        e = self.entries[str(t.rollout_a)]
        frozen = self.freeze / e["physical"]
        self.probes = getattr(self, "probes", 0) + 1
        results = self.base / f"probe-results-{self.probes}"
        results.mkdir(parents=True, exist_ok=True)
        scratch = self.base / "probe-scratch"
        scratch.mkdir(exist_ok=True)
        names = sorted(n.name for n in (self.freeze / self.entries[str(t.sessions)]["physical"]).iterdir())
        logical = lambda p: str(p) if home_override is None else str(p).replace(
            str(t.home), str(home_override))
        plan = {
            "home": logical(t.home), "codexRoot": logical(t.codex),
            "sessionsDir": logical(t.sessions),
            "transcript": {"logical": logical(t.rollout_a), "sha256": _sha(frozen),
                           "identity": [e["dev"], e["ino"], e["admittedLength"],
                                        e["mtimeNs"]]},
            "frozenNames": names, "link": logical(t.link),
            "linkTarget": self.entries[str(t.link)]["target"],
            "auth": {"logical": logical(t.auth),
                     "sha256": self.entries[str(t.auth)]["sha256"]},
            "absent": logical(t.hooks), "results": str(results),
            "state": logical(t.state), "stateThread": t.thread_a,
            "stateFrozenDir": str((self.freeze / self.entries[str(t.state)]["physical"]).parent),
            "scratch": str(scratch), "bypass": self.bypass,
        }
        # Amendment 10: one out-of-root rollout link (the target keeps its own
        # logical path outside HOME) and the memory directory link
        tg = self.entries[str(t.targets[0])]
        plan["fileLink"] = {
            "logical": logical(t.links[0]), "target": str(t.targets[0]),
            "dir": logical(t.linked_dir), "name": t.links[0].name,
            "sha256": _sha(self.freeze / tg["physical"]),
            "identity": [tg["dev"], tg["ino"], tg["admittedLength"], tg["mtimeNs"]],
            "linkIno": self.entries[str(t.links[0])]["ino"]}
        meta = next((d.get("metadata") or {} for d in self.manifest.get("deniedTargets") or []
                     if d["logical"] == str(t.memory_target)), {})
        plan["dirLink"] = {"logical": logical(t.memory), "target": str(t.memory_target),
                           "identity": [meta.get("dev"), meta.get("ino"), meta.get("mode"),
                                        meta.get("mtimeNs")]}
        plan["claudeProjects"] = logical(t.claude.parent)
        plan["claudeTranscripts"] = sorted(
            logical(e["logical"]) for e in self.manifest["entries"]
            if e["kind"] == "file" and e["logical"].startswith(str(t.claude.parent) + "/"))
        path = results / "plan.json"
        plan["planPath"] = str(path)
        path.write_text(json.dumps(plan))
        return plan

    def coverage(self, red: bool) -> None:
        self.bypass = self.build_bypass()
        env = env_for(self.tree, self.base / "unused")
        if red:   # the current runners: no namespace, the live paths, no sandbox
            plan = self.plan()
            out = run([PY, __file__, "probe", "--plan", plan["planPath"]], env=env,
                      timeout=120)
            res = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
            self.check("probe.frozenContent", res.get("frozenContent"), res)
            relocated = self.base / "relocated" / "home"
            for e in self.manifest["entries"]:
                if e["kind"] == "file" and e["logical"].startswith(str(self.tree.home)):
                    dst = relocated / os.path.relpath(e["logical"], self.tree.home)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.freeze / e["physical"], dst)
            st = os.stat(relocated / os.path.relpath(self.tree.rollout_a, self.tree.home))
            e = self.entries[str(self.tree.rollout_a)]
            self.check("relocated.identityPreserved", st.st_ino == e["ino"],
                       f"the copied file's inode {st.st_ino} differs from the "
                       f"source's {e['ino']}: the walkers' replacement check "
                       "would treat it as a new file")
            raw = run([self.bypass, "raw", str(self.tree.rollout_a)], timeout=30)
            self.check("bypass.rawSyscallDenied", raw.returncode == -9,
                       f"a direct open of the live path returned {raw.stdout.strip()} "
                       "(nothing denies it)")
            raw = run([self.bypass, "raw", str(self.tree.memory / self.tree.memory_file.name)],
                      timeout=30)
            self.check("bypass.rawThroughDirLinkKilled", raw.returncode == -9,
                       f"a direct open through the memory link returned "
                       f"{raw.stdout.strip()} (nothing denies its live target)")
            return
        plan = self.plan()
        receipts = self.base / "probe-receipts"
        out = self.launch([PY, __file__, "probe", "--plan", plan["planPath"]], env,
                          receipts, timeout=180)
        res = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
        if out.returncode != 0:
            self.check("probe.ran", False, out.stderr[-1500:])
        for key, value in sorted(res.items()):
            self.check(f"probe.{key}", value, res)
        fam = self.family(receipts)
        self.check("probe.familyValid", fam["valid"], fam["problems"])
        self.check("probe.systemProgramAdmitted", any(
            a.get("program") == "/usr/bin/env"
            and a.get("argv") == ["/usr/bin/env", "/usr/bin/true"]
            and (a.get("termination") or {}).get("code") == 0
            for a in fam.get("admittedSystemPrograms") or []),
                   fam.get("admittedSystemPrograms"))
        events = _receipt_events(receipts)
        self.check("probe.reinjectionRecorded",
                   any(e.get("reinjected") is True for e in events), "no re-injection")
        self.check("probe.forkActivations",
                   sum(1 for e in events if e.get("event") == "activate"
                       and e.get("image") == "fork") >= 3, "fork children not activated")
        self.check("probe.osExitRecorded",
                   any(e.get("event") == "exit" and e.get("how") == "_exit"
                       for e in events), "no os._exit receipt line")
        # Amendment 13 O1: following the non-traversed directory link, in its
        # own launch (before the fix the sandbox killed it at os.walk)
        receipts = self.base / "dirlink-receipts"
        out = self.launch([PY, __file__, "dirlink", "--plan", plan["planPath"]], env,
                          receipts, timeout=120)
        res = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
        self.check("dirlink.ran", out.returncode == 0,
                   (out.returncode, out.stdout[-300:], out.stderr[-1200:]))
        for key, value in sorted(res.items()):
            self.check(f"dirlink.{key}", value, res)
        fam = self.family(receipts)
        self.check("dirlink.familyValid", fam["valid"], fam["problems"])
        # Amendment 13 O2: the product's project resolution of the worktree
        # session through the namespace, after Codex removed the worktree,
        # equals its native control at capture time (and differs from a
        # native resolution now, so the frozen answers decide it)
        cwd = str(self.tree.worktree_cwd)
        receipts = self.base / "project-receipts"
        out = self.launch([PY, __file__, "project", "--tree", str(self.tree_dir),
                           "--cwd", cwd], env, receipts, timeout=120)
        frozen = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
        self.check("project.frozenEqualsNativeControl", frozen and frozen == self.native_project,
                   {"frozen": frozen, "control": self.native_project,
                    "rc": out.returncode, "stderr": out.stderr[-500:]})
        now = run([PY, __file__, "project", "--tree", str(self.tree_dir), "--cwd", cwd],
                  env=env, timeout=60)
        self.check("project.liveNowDiffers", now.returncode == 0
                   and json.loads(now.stdout or "{}") != self.native_project, now.stdout)
        fam = self.family(receipts)
        self.check("project.familyValid", fam["valid"], fam["problems"])
        # Amendment 14 P1: the same for the Claude sessions whose cwd is the
        # memory link's denied target and a directory below it, after the
        # target's `.git` and the directories below it were removed: the
        # walk-up reads the probe entries and absences inside the target
        for name, (cwd, control) in sorted(self.native_target_projects.items()):
            receipts = self.base / f"project-{name}-receipts"
            out = self.launch([PY, __file__, "project", "--tree", str(self.tree_dir),
                               "--cwd", cwd], env, receipts, timeout=120)
            frozen = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
            self.check(f"project.{name}.frozenEqualsNativeControl",
                       frozen and frozen == control,
                       {"frozen": frozen, "control": control,
                        "rc": out.returncode, "stderr": out.stderr[-500:]})
            now = run([PY, __file__, "project", "--tree", str(self.tree_dir), "--cwd", cwd],
                      env=env, timeout=60)
            self.check(f"project.{name}.liveNowDiffers", now.returncode == 0
                       and json.loads(now.stdout or "{}") != control, now.stdout)
            fam = self.family(receipts)
            self.check(f"project.{name}.familyValid", fam["valid"], fam["problems"])
        # 8. bypasses: the library unloaded (SIP shell strips it), a raw syscall
        killed = self.launch(["/bin/sh", "-c", f'exec {PY} -c "import os; '
                              f"os.stat('{self.tree.rollout_a}'); print('read')\""],
                             env, self.base / "bypass-receipts")
        self.check("bypass.unloadedLibraryKilled",
                   killed.returncode == -9 and "read" not in killed.stdout,
                   (killed.returncode, killed.stdout, killed.stderr[-300:]))
        raw = self.launch([self.bypass, "raw", str(self.tree.rollout_a)], env,
                          self.base / "bypass-receipts")
        self.check("bypass.rawSyscallDenied", raw.returncode == -9,
                   (raw.returncode, raw.stdout))

    # -- 9. invalidity ----------------------------------------------------------
    def invalidity(self) -> None:
        t = self.tree
        env = env_for(t, self.base / "unused")
        code = ("import os,sys\np=sys.argv[1]\n"
                "try: open(p, sys.argv[2]).close()\nexcept OSError as e: print(e.errno)\n")
        for name, path, mode in (("missingMapping", t.history, "r"),
                                 ("writeIntent", t.rollout_a, "a")):
            rec = self.base / f"inv-{name}"
            out = self.launch([PY, "-c", code, str(path), mode], env, rec, timeout=60)
            fam = self.family(rec)
            self.check(f"invalid.{name}", out.returncode == 0 and not fam["valid"]
                       and out.stdout.strip() == "13", (out.stdout, fam["problems"]))
        # a denied required read: the product itself reads an unmapped input
        rec = self.base / "inv-deniedRead"
        narrow = self.capture_extra("freeze-narrow")
        tsv = (narrow / "rootmap.tsv").read_text()
        os.chmod(narrow, 0o755)
        os.chmod(narrow / "rootmap.tsv", 0o644)
        dropped = (str(t.auth), str(t.config), str(t.claude_json))
        (narrow / "rootmap.tsv").write_text("\n".join(
            l for l in tsv.splitlines()
            if not (l.startswith("E\t") and l.split("\t")[2] in dropped)) + "\n")
        data = self.base / "inv-data"
        clone(self.src / "data", data)
        out = self.launch([PY, self.cctally, "cache-sync", "--source", "all"],
                          env_for(t, data), rec, freeze=narrow)
        fam = self.family(rec, narrow)
        self.check("invalid.deniedRequiredRead", not fam["valid"] and any(
            "unmapped" in p for p in fam["problems"]),
                   (out.returncode, fam["problems"]))
        # a swallowed sandbox kill (Q17): a covered child bypasses the library
        # with a raw open of a live path, the sandbox kills it (SIGKILL, no
        # log), and its parent ignores the failure and exits 0
        rec = self.base / "inv-swallowedKill"
        swallow = ("import subprocess,sys\n"
                   "r = subprocess.run([sys.argv[1], 'raw', sys.argv[2]],"
                   " capture_output=True, timeout=30)\n"
                   "print(r.returncode)\n")
        out = self.launch([PY, "-c", swallow, self.bypass, str(t.rollout_a)], env,
                          rec, timeout=60)
        fam = self.family(rec)
        self.check("invalid.swallowedSandboxKill", out.returncode == 0
                   and out.stdout.strip() == "-9" and not fam["valid"]
                   and any("signal 9" in p for p in fam["problems"]),
                   (out.returncode, out.stdout, fam["problems"]))
        # Amendments 10 and 13: a deliberate traversal of the non-traversed
        # directory link. Listing it (or its target) through the namespace is
        # refused with EACCES and recorded as a `traversal` (O1: no kernel
        # call reaches the live target), as is opening a file under it; and a
        # raw open through the live link or of the live target (no library)
        # is still killed by the sandbox, the backstop. Each is INVALID.
        lister = ("import os,sys\n"
                  "try: print(os.listdir(sys.argv[1]))\n"
                  "except OSError as e: print(e.errno)\n")
        for name, path in (("dirLinkListingRefused", t.memory),
                           ("dirLinkTargetListingRefused", t.memory_target)):
            rec = self.base / f"inv-{name}"
            out = self.launch([PY, "-c", lister, str(path)], env, rec, timeout=60)
            fam = self.family(rec)
            self.check(f"invalid.{name}", out.returncode == 0
                       and out.stdout.strip() == "13" and not fam["valid"]
                       and any("traversal" in p for p in fam["problems"]),
                       (out.returncode, out.stdout, out.stderr[-300:], fam["problems"]))
        # O2: a probe entry is metadata only - opening it is refused
        rec = self.base / "inv-probeOpen"
        out = self.launch([PY, "-c", code, str(t.worktree / ".git"), "r"], env, rec,
                          timeout=60)
        fam = self.family(rec)
        self.check("invalid.probeOpenRefused", out.returncode == 0
                   and out.stdout.strip() == "13" and not fam["valid"]
                   and any("unmapped-open" in p for p in fam["problems"]),
                   (out.returncode, out.stdout, fam["problems"]))
        # Amendment 14 P1: inside the denied target a probe is still metadata
        # only (listing one is `unmapped-open`), and any other access below
        # the target - a stat of a file there - stays a `traversal`
        rec = self.base / "inv-targetProbeListing"
        out = self.launch([PY, "-c", lister, str(t.memory_below.parent)], env, rec,
                          timeout=60)
        fam = self.family(rec)
        self.check("invalid.targetProbeListingRefused", out.returncode == 0
                   and out.stdout.strip() == "13" and not fam["valid"]
                   and any("unmapped-open" in p for p in fam["problems"])
                   and not any("traversal" in p for p in fam["problems"]),
                   (out.returncode, out.stdout, fam["problems"]))
        stater = ("import os,sys\n"
                  "try: os.stat(sys.argv[1]); print('ok')\n"
                  "except OSError as e: print(e.errno)\n")
        rec = self.base / "inv-belowTargetStat"
        out = self.launch([PY, "-c", stater, str(t.memory_file)], env, rec, timeout=60)
        fam = self.family(rec)
        self.check("invalid.belowTargetStatTraversal", out.returncode == 0
                   and out.stdout.strip() == "13" and not fam["valid"]
                   and any("traversal" in p for p in fam["problems"]),
                   (out.returncode, out.stdout, fam["problems"]))
        rec = self.base / "inv-dirLinkOpen"
        out = self.launch([PY, "-c", code, str(t.memory / t.memory_file.name), "r"], env,
                          rec, timeout=60)
        fam = self.family(rec)
        self.check("invalid.dirLinkOpenTraversal", out.returncode == 0
                   and out.stdout.strip() == "13" and not fam["valid"]
                   and any("traversal" in p for p in fam["problems"]),
                   (out.returncode, out.stdout, fam["problems"]))
        raw = self.launch([self.bypass, "raw", str(t.memory / t.memory_file.name)], env,
                          self.base / "inv-dirLinkRaw")
        self.check("bypass.rawThroughDirLinkKilled", raw.returncode == -9,
                   (raw.returncode, raw.stdout))
        raw = self.launch([self.bypass, "raw", str(t.targets[0])], env,
                          self.base / "inv-linkTargetRaw")
        self.check("bypass.rawLinkTargetKilled", raw.returncode == -9,
                   (raw.returncode, raw.stdout))
        # a lost receipt
        lost = self.base / "inv-lost"
        shutil.copytree(self.receipts, lost)
        launched = json.loads((lost / "launches.jsonl").read_text().splitlines()[0])["pid"]
        for p in lost.glob(f"{launched}.*.jsonl"):
            p.unlink()
        fam = self.family(lost)
        self.check("invalid.lostReceipt", not fam["valid"] and any(
            "never activated" in p for p in fam["problems"]), fam["problems"])
        # absent enforcement: the library loaded without the sandbox
        rec = self.base / "inv-unenforced"
        rec.mkdir()
        envx = dict(env, DYLD_INSERT_LIBRARIES=self.rootmap,
                    ROOTMAP_MANIFEST=str(self.freeze / "rootmap.tsv"),
                    ROOTMAP_RECEIPTS=str(rec))
        run([PY, "-c", "pass"], env=envx, timeout=60)
        fam = self.family(rec)
        self.check("invalid.absentEnforcement", not fam["valid"] and any(
            "absent enforcement" in p for p in fam["problems"]), fam["problems"])
        refused = run([PY, str(HERE / "frozen_launch.py"), "--freeze", str(self.freeze),
                       "--rootmap", self.rootmap, "--receipts", str(rec), "--",
                       PY, "-c", "pass"], env=env, timeout=60)
        self.check("invalid.launcherRefusesWithoutSandbox", refused.returncode == 2,
                   refused.stderr)
        # a changed frozen prefix: an in-place byte change with the size, mode
        # and mtime restored is invisible to the metadata seal; --full sees it
        sealed = self.capture_extra("freeze-prefix")
        manifest = json.loads((sealed / "manifest.json").read_text())
        entry = next(e for e in manifest["entries"] if e["logical"] == str(t.rollout_a))
        victim = sealed / entry["physical"]
        st = os.stat(victim)
        os.chmod(victim, 0o644)
        with open(victim, "r+b") as fh:
            fh.write(b"X")
        os.chmod(victim, stat_mod.S_IMODE(st.st_mode))
        os.utime(victim, ns=(st.st_atime_ns, st.st_mtime_ns))
        out = run([PY, str(HERE / "frozen_roots.py"), "verify", "--freeze", str(sealed),
                   "--full"], timeout=60)
        self.check("invalid.changedPrefix", out.returncode == 2
                   and "contentDigest" in out.stdout, out.stdout)
        # a changed inode: qualification refuses a store retaining another inode
        store = self.base / "src-other-inode"
        shutil.copytree(self.src, store)
        conn = sqlite3.connect(store / "data" / "cache.db")
        conn.execute("UPDATE codex_session_files SET inode = inode + 1 WHERE path = ?",
                     (str(t.rollout_a),))
        conn.commit()
        conn.close()
        out = run([PY, str(HERE / "frozen_roots.py"), "qualify", "--store", str(store),
                   "--freeze", str(self.freeze), "--out",
                   str(self.base / "qualify-other.json")], timeout=60)
        self.check("invalid.changedInode", out.returncode == 2 and "replaced" in out.stdout,
                   out.stdout)
        # a timeout: a bounded family member that never finishes
        try:
            self.launch([PY, "-c", "import time; time.sleep(30)"], env,
                        self.base / "inv-timeout", timeout=2)
            self.check("invalid.timeout", False, "the bound did not fire")
        except Bounded as exc:
            self.check("invalid.timeout", True, str(exc))


    # -- 10. top-level family processes (Amendment 13 O3) --------------------
    def toplevel_ends(self) -> None:
        """A top-level family process's parent is the runner's shell, which
        the namespace does not load, so its own end is recorded by the
        runner: through the real `_inputs.sh` helpers in frozen mode, a
        top-level process the sandbox kills makes its family INVALID, while
        a harness teardown kill (`wa_kill`) and a normal exit do not."""
        killed, torn = self.base / "top-killed", self.base / "top-teardown"
        ready = self.base / "top-teardown.ready"
        script = self.base / "toplevel-runner.sh"
        # HR-10: the teardown kill waits for the process's own readiness file
        # (written by its final image, Homebrew's python3 stub having exec'd
        # the framework binary) and a complete activation after its last exec,
        # never for the receipt file merely to exist.
        script.write_text("\n".join([
            "set -u", f"P={HERE}", '. "$P/_inputs.sh"', f"ROOTMAP={self.rootmap}",
            f"WA_RECEIPTS={killed}",
            f"( wa_exec {self.bypass} raw {self.tree.rollout_a} ) > /dev/null 2>&1 &",
            'wa_wait killed $!; echo "killed=$?"',
            f"WA_RECEIPTS={torn}",
            f"( wa_exec {PY} -c 'import pathlib, sys, time; "
            f"pathlib.Path(sys.argv[1]).write_text(\"ready\"); time.sleep(60)' "
            f"{ready} ) > /dev/null 2>&1 &",
            "p=$!; n=0; trap '[ -n \"$p\" ] && kill -9 $p 2>/dev/null' EXIT",
            f'while [ $n -lt 300 ] && ! {{ [ -e {ready} ] && {PY} {HERE}/frozen_roots.py ready '
            '--receipts "$WA_RECEIPTS" --pid $p; }; do '
            "sleep 0.1; n=$((n+1)); done",
            'wa_kill 9 $p; wa_wait teardown $p; echo "teardown=$?"; p=',
            f"( wa_exec {PY} -c pass ) > /dev/null 2>&1 &",
            'wa_wait normal $!; echo "normal=$?"']) + "\n")
        env = dict(env_for(self.tree, self.base / "unused"),
                   WRITE_ATTRIBUTION_INPUTS=f"frozen:{self.freeze}")
        out = run(["/bin/bash", str(script)], env=env, timeout=120)
        self.facts["toplevelRunner"] = {"returncode": out.returncode,
                                        "stdout": out.stdout, "stderr": out.stderr[-500:]}
        self.check("toplevel.runnerStatuses", out.stdout.split() == [
            "killed=137", "teardown=137", "normal=0"], self.facts["toplevelRunner"])
        fam = self.family(killed)
        self.check("invalid.killedTopLevel", not fam["valid"] and any(
            "top-level pid" in p and "signal 9" in p for p in fam["problems"]),
                   fam["problems"])
        fam = self.family(torn)
        self.check("toplevel.teardownAndNormalValid", fam["valid"]
                   and fam.get("teardownKills", 0) >= 1
                   and (fam.get("topLevel") or {}).get("records") == 2,
                   (fam["problems"], fam.get("topLevel")))

    def run(self) -> int:
        red = self.args.red
        try:
            if not red:
                self.measured_facts()
            self.prepare()
            self.capture()
            self.control()
            self.tree.mutate_after_capture()
            self.frozen_run(red)
            self.coverage(red)
            if not red:
                self.invalidity()
                self.toplevel_ends()
        except (Bounded, RuntimeError, OSError, ValueError, KeyError,
                sqlite3.Error) as exc:
            self.check("selftest.completed", False, f"{type(exc).__name__}: {exc}")
        finally:
            self.tree.close_state()
        return self.report()

    def report(self) -> int:
        failed = [c for c in self.checks if not c["ok"]]
        result = {"schema": "frozen-selftest/1", "red": self.args.red,
                  "tree": str(self.tree_dir), "python": sys.version.split()[0],
                  "rootmap": {"path": self.rootmap, "sha256": _sha(self.rootmap)},
                  "wtrace": {"path": self.wtrace, "sha256": _sha(self.wtrace)},
                  "facts": self.facts, "checks": self.checks,
                  "passed": not failed}
        if self.args.out:
            pathlib.Path(self.args.out).write_text(json.dumps(result, indent=1) + "\n")
        for c in failed:
            print(f"frozen-selftest: FAIL {c['check']}: {c['detail'][:400]}")
        print(f"frozen-selftest: {len(self.checks) - len(failed)}/{len(self.checks)} checks")
        print("frozen-selftest:", "PASS" if not failed else "FAIL")
        if not self.args.keep:
            run(["chmod", "-R", "u+w", str(self.base)], timeout=120)
            shutil.rmtree(self.base, ignore_errors=True)
        return 0 if not failed else 1


def _sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _receipt_events(directory) -> list:
    out = []
    for p in pathlib.Path(directory).glob("*.jsonl"):
        if p.name == "launches.jsonl":
            continue
        out += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["project"]:
        p = argparse.ArgumentParser()
        p.add_argument("--tree", required=True)
        p.add_argument("--cwd", required=True)
        a = p.parse_args(argv[1:])
        print(json.dumps(project_resolution(a.tree, a.cwd)))
        return 0
    if argv[:1] in (["probe"], ["child"], ["dirlink"]):
        p = argparse.ArgumentParser()
        p.add_argument("--plan", required=True)
        p.add_argument("--out")
        a = p.parse_args(argv[1:])
        plan = json.loads(pathlib.Path(a.plan).read_text())
        if argv[0] == "child":
            _dump(a.out, child_checks(plan))
            return 0
        print(json.dumps(dirlink_checks(plan) if argv[0] == "dirlink" else probe(plan)))
        return 0
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--rootmap", required=True)
    parser.add_argument("--wtrace", required=True)
    parser.add_argument("--out")
    parser.add_argument("--red", action="store_true")
    parser.add_argument("--keep", action="store_true")
    return SelfTest(parser.parse_args(argv)).run()


if __name__ == "__main__":
    raise SystemExit(main())
