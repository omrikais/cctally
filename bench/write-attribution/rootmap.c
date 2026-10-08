// #901 spec §6.3 "Frozen namespace" (Q16, `dc13` L1): a benchmark-only read
// namespace. Loaded beside wtrace.dylib (DYLD_INSERT_LIBRARIES =
// rootmap.dylib:wtrace.dylib, composed by frozen_launch.py inside the run's
// sandbox), it redirects every read of a path under a frozen logical root to
// the sealed freeze and answers with the CAPTURED source identity, so the
// tested tree's stored root keys, file keys, physical path strings, inodes and
// committed prefixes still match and nothing re-ingests or prunes because of
// the relocation. It never interposes a write function (wtrace.c's accounting
// is unchanged) and it writes its own receipts through raw system calls, so
// none of its bytes reach wtrace's per-path table.
//
// Manifest: $ROOTMAP_MANIFEST, the freeze's rootmap.tsv (frozen_roots.py
// capture writes it; tab-separated, one record per line). META below is the
// 16 fields dev ino size mode nlink uid gid atime_s atime_ns mtime_s
// mtime_ns ctime_s ctime_ns btime_s btime_ns 0:
//   ROOTMAP 2                                 (version 1 is refused: recapture)
//   R <logical root> <frozen root> <is-file 0|1> <present 0|1>
//   E <F|D|L> <logical path> META
//     [<symlink target as stored> <resolved logical target>]
//   E P <logical path> META [<link target> <resolved target or "">]
//                                             a PROBE entry (Amendment 13
//                                             O2): a path under a root the
//                                             family only checks (the git-root
//                                             walk-up from a recorded cwd);
//                                             metadata only, no frozen file
//   S <logical path> META                     what a following stat of the
//                                             probe link at that path presents
//   A <logical path>                          a recorded absence
//   X <live path> [META]                      the live target of a
//                                             non-traversed directory link
//                                             (Amendment 10): denied by the
//                                             profile, never mapped or
//                                             opened; META is the target's
//                                             own captured lstat (Amendment
//                                             13 O1)
// `size` of a file is its ADMITTED length (the frozen copy's length). The
// target of a covered link outside every root is its own file root (R ... 1),
// so a freeze holds as many roots as the real roots hold such links (95).
// An L entry whose resolved target is an X path is a NON-TRAVERSED
// DIRECTORY LINK: its frozen link points at the live target (any kernel
// follow is killed by the sandbox), so a following stat / fstatat / access
// of it, and a stat / lstat / access / realpath of the target path itself,
// are answered from the X record's metadata with no kernel call on the live
// target; realpath through it returns the logical target untouched; any
// open, directory listing, chdir or readlink of the target, and any access
// below the link or the target, is refused (EACCES, a "traversal" event).
// A P entry is answered from its metadata alone (stat / lstat / fstatat /
// access, readlink of a probe link, realpath) with no kernel call; an open,
// directory listing or chdir of it is refused (EACCES, "unmapped-open"), and
// a path below it that is not itself an entry is "unmapped". P entries and
// recorded absences may also lie INSIDE a denied target (Amendment 14 P1:
// the git-root walk-up from a recorded cwd at or below a memory link's
// target): there a P entry is answered exactly so, a path at or under an
// absence is ENOENT, and every other path below the target stays a
// "traversal"; the target itself keeps the X record's presentation.
//
// Interposed (arm64, CPython 3.14.7 + this SDK; there are no $INODE64 forms
// on arm64): open, open$NOCANCEL, openat, openat$NOCANCEL, stat, lstat, fstat,
// fstatat, access, faccessat, opendir, __opendir2, fdopendir, readdir,
// readdir_r, readlink, readlinkat, realpath, realpath$DARWIN_EXTSN,
// fcntl(F_GETPATH / F_GETPATH_NOFIRMLINK), chdir, getcwd, execve and
// posix_spawn (re-inject the namespace into a child whose environment lost
// it; each launch is recorded with its program, arguments, parent and child
// pid), waitpid, wait4, wait3, wait and waitid (the parent records every
// child's termination status, so a child the sandbox kills is visible even
// when its parent ignores the failure - spec §6.3 "Frozen namespace", Q17),
// kill and killpg (which family member sent a signal, for the diagnosis),
// _exit (final receipt line on os._exit), plus a fork handler (every forked
// child writes its own activation). getattrlist, fgetattrlist,
// getattrlistbulk and statx_np are NOT imported by CPython or its extension
// modules (nm -u), so the tree cannot reach them; the sandbox kills any
// process that touches a live root through a call this library misses.
//
// Policy for a path under a logical root (or already under its frozen root):
//   recorded entry  -> the call runs on the frozen path; stat/fstat/readdir
//                      return the captured dev, ino, size, mode, nlink, uid,
//                      gid and timestamps; realpath/getcwd/F_GETPATH return the
//                      logical spelling; readlink of a link's logical spelling
//                      returns the stored target, of its frozen spelling the
//                      frozen link (so libc's realpath stays in the freeze)
//   recorded absence (or under one) -> ENOENT
//   a non-traversed directory link, its live target, or anything under
//   either -> see the manifest notes above ("traversal" events)
//   anything else   -> EACCES and an "unmapped" receipt event (fail closed)
//   any write intent (open for writing, access W_OK) -> EACCES and a
//                      "write-denied" event: the freeze is never written
// Every other path, the databases, scratch roots and TMPDIR pass through.
//
// Receipts: $ROOTMAP_RECEIPTS/<pid>.<start_s>.<start_us>.jsonl, one per
// process (exec keeps the file: the start time survives exec), JSON lines:
//   {"event":"activate","schema":"rootmap/1","image":"load"|"fork",...}
//   {"event":"unmapped"|"write-denied"|"unmapped-dirent"|"unmapped-fd"|
//            "traversal"|"unmapped-open",...}
//   {"event":"exec"|"exec-failed"|"spawn"|"spawn-failed","target":...,
//    ["interpreter":...,] "ppid":n, ["child":n,] "argv":[...],
//    "argvTruncated":bool, "reinjected":bool}
//   {"event":"reaped","call":"waitpid"|...,"child":n,"status":n,
//    "exited":bool,"code":n|null,"signaled":bool,"signal":n|null}
//   {"event":"signal-sent","call":"kill"|"killpg","target":n,"signal":n}
//   {"event":"exit","how":"exit"|"_exit","counters":{...}}
// frozen_roots.py `family` judges a run's receipts.
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <dirent.h>
#include <spawn.h>
#include <signal.h>
#include <pthread.h>
#include <dlfcn.h>
#include <stdatomic.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <sys/param.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <sys/resource.h>
#include <libproc.h>
#include <sys/proc_info.h>
#include <CommonCrypto/CommonDigest.h>

#pragma clang diagnostic ignored "-Wdeprecated-declarations"

#define DYLD_INTERPOSE(_r, _e) \
  __attribute__((used)) static struct { const void *r; const void *e; } _interpose_##_e \
  __attribute__((section("__DATA,__interpose"))) = {(const void *)(unsigned long)&_r, (const void *)(unsigned long)&_e};

// ABI variants the C names do not reach.
extern int open_nocancel_v(const char *, int, ...) __asm("_open$NOCANCEL");
extern int openat_nocancel_v(int, const char *, int, ...) __asm("_openat$NOCANCEL");
extern char *realpath_plain(const char *, char *) __asm("_realpath");
extern char *realpath_ext(const char *, char *) __asm("_realpath$DARWIN_EXTSN");
extern DIR *__opendir2(const char *, int);
extern int sandbox_check(pid_t, const char *, int, ...);
extern const int SANDBOX_CHECK_NO_REPORT __attribute__((weak_import));

#ifndef F_GETPATH_NOFIRMLINK
#define F_GETPATH_NOFIRMLINK 102
#endif

// ── manifest ─────────────────────────────────────────────────────────────
struct root {
  const char *logical; size_t llen;
  const char *frozen; size_t flen;
  char canon[MAXPATHLEN]; size_t clen;   // realpath of the frozen root
  int is_file, present;
};
struct meta {
  unsigned long long dev, ino, size, mode, nlink, uid, gid;
  struct timespec at, mt, ct, bt;
};
struct ent {
  const char *path; char kind;            // 'F' 'D' 'L' 'A' 'P'
  struct meta m;
  const char *target, *resolved;          // symlinks only (a probe link too)
  int has_fo; struct meta fo;             // 'P' link: what a following stat presents
  int dirlink;                            // 'L': a non-traversed directory link
  int tgt;                                // ... and its X record
  int next;
};
// The X records: live targets of non-traversed directory links (Amendment
// 10), presented from their captured metadata (Amendment 13 O1).
struct target {
  const char *path; size_t len;
  int has_meta; struct meta m;
};
static struct root *roots;               // one per R record (heap)
static int nroots;
static struct target *targets;           // the X records (heap)
static int ntargets;
static struct ent *ents;
static int nents, nbuckets;
static int *buckets;
static int load_ok;                       // 1 loaded, 0 not configured, -1 failed
static char load_error[256];
static char manifest_path[MAXPATHLEN];
static char manifest_sha[65];
static dev_t frozen_dev;
static int have_frozen_dev;

// saved launch environment (for re-injection into stripped children)
static char self_path[MAXPATHLEN];
static char env_manifest[MAXPATHLEN + 32];
static char env_receipts[MAXPATHLEN + 32];
static char receipts_dir[MAXPATHLEN];

// ── counters ─────────────────────────────────────────────────────────────
static _Atomic unsigned long long c_mapped, c_frozen_direct, c_absent, c_unmapped,
  c_write_denied, c_dirent, c_fd_patched, c_reverse, c_execs, c_spawns, c_reinjected,
  c_events_dropped, c_reaped, c_signals, c_target, c_traversal, c_probe;

// ── receipts (raw system calls: invisible to wtrace) ─────────────────────
static int rfd = -1;
static dev_t rdev;
static ino_t rino;
static pthread_mutex_t rmu = PTHREAD_MUTEX_INITIALIZER;
static int exit_written;
static unsigned events_logged;
#define MAX_EVENTS 256

static long raw_open(const char *p, int flags, int mode) { return syscall(SYS_open, p, flags, mode); }
static void raw_write(int fd, const char *b, size_t n) {
  while (n > 0) { long w = syscall(SYS_write, fd, b, n); if (w <= 0) return; b += w; n -= (size_t)w; }
}

struct buf { char s[6 * MAXPATHLEN]; size_t n; };
static void bput(struct buf *b, const char *s) {
  while (*s && b->n + 1 < sizeof b->s) b->s[b->n++] = *s++;
  b->s[b->n] = 0;
}
static void bnum(struct buf *b, unsigned long long v) {
  char t[24]; int i = 0;
  if (!v) t[i++] = '0';
  while (v) { t[i++] = (char)('0' + v % 10); v /= 10; }
  while (i > 0 && b->n + 1 < sizeof b->s) b->s[b->n++] = t[--i];
  b->s[b->n] = 0;
}
// A JSON string literal of at most `max` input bytes.
static void bstrn(struct buf *b, const char *s, size_t max) {
  bput(b, "\"");
  for (size_t i = 0; s && *s && i < max && b->n + 8 < sizeof b->s; s++, i++) {
    unsigned char c = (unsigned char)*s;
    if (c == '"' || c == '\\') { b->s[b->n++] = '\\'; b->s[b->n++] = (char)c; }
    else if (c < 0x20) { const char *hx = "0123456789abcdef"; b->s[b->n++] = '\\'; b->s[b->n++] = 'u';
      b->s[b->n++] = '0'; b->s[b->n++] = '0'; b->s[b->n++] = hx[c >> 4]; b->s[b->n++] = hx[c & 15]; }
    else b->s[b->n++] = (char)c;
  }
  b->s[b->n] = 0;
  bput(b, "\"");
}
static void bstr(struct buf *b, const char *s) { bstrn(b, s, (size_t)-1); }
// argv as a JSON array: at most 64 arguments of at most 512 bytes each, and
// never past the room the rest of the event needs; "argvTruncated" says so.
static void bargv(struct buf *b, char *const argv[]) {
  int truncated = 0;
  bput(b, ",\"argv\":[");
  for (int i = 0; argv && argv[i]; i++) {
    if (i >= 64 || b->n + 1200 > sizeof b->s) { truncated = 1; break; }
    if (strlen(argv[i]) > 512) truncated = 1;
    if (i) bput(b, ",");
    bstrn(b, argv[i], 512);
  }
  bput(b, "],\"argvTruncated\":"); bput(b, truncated ? "true" : "false");
}
static void btime(struct buf *b) {
  struct timeval tv; gettimeofday(&tv, NULL);
  bnum(b, (unsigned long long)tv.tv_sec); bput(b, ".");
  char u[8]; int us = (int)tv.tv_usec;
  for (int i = 5; i >= 0; i--) { u[i] = (char)('0' + us % 10); us /= 10; }
  u[6] = 0; bput(b, u);
}
static void bhead(struct buf *b, const char *event) {
  b->n = 0; b->s[0] = 0;
  bput(b, "{\"event\":"); bstr(b, event);
  bput(b, ",\"pid\":"); bnum(b, (unsigned long long)getpid());
  bput(b, ",\"t\":"); btime(b);
}

static char receipt_path[MAXPATHLEN];

static void open_receipt(void) {
  if (!receipts_dir[0]) return;
  struct proc_bsdinfo info;
  unsigned long long ss = 0, su = 0;
  if (proc_pidinfo(getpid(), PROC_PIDTBSDINFO, 0, &info, sizeof info) == (int)sizeof info) {
    ss = info.pbi_start_tvsec; su = info.pbi_start_tvusec;
  }
  struct buf b; b.n = 0; b.s[0] = 0;
  bput(&b, receipts_dir); bput(&b, "/"); bnum(&b, (unsigned long long)getpid());
  bput(&b, "."); bnum(&b, ss); bput(&b, "."); bnum(&b, su); bput(&b, ".jsonl");
  snprintf(receipt_path, sizeof receipt_path, "%s", b.s);
  rfd = (int)raw_open(b.s, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
  struct stat st;
  if (rfd >= 0 && fstat(rfd, &st) == 0) { rdev = st.st_dev; rino = st.st_ino; }
}

// A forked child's close_fds (subprocess) or the product itself may close
// the receipt descriptor, and its number may then name another file: check
// the descriptor is still the receipt before every write, else reopen it.
static int ensure_rfd(void) {
  struct stat st;
  if (rfd >= 0 && fstat(rfd, &st) == 0 && st.st_dev == rdev && st.st_ino == rino) return rfd;
  if (!receipt_path[0]) return -1;
  rfd = (int)raw_open(receipt_path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
  if (rfd >= 0 && fstat(rfd, &st) == 0) { rdev = st.st_dev; rino = st.st_ino; }
  return rfd;
}

static void emit(struct buf *b) {
  bput(b, "}\n");
  if (ensure_rfd() >= 0) raw_write(rfd, b->s, b->n);
}

// One bounded event (the first MAX_EVENTS per process are logged in full;
// the rest are counted in the exit line's eventsDropped).
static void event_path(const char *event, const char *call, const char *path) {
  pthread_mutex_lock(&rmu);
  if (events_logged >= MAX_EVENTS) { c_events_dropped++; pthread_mutex_unlock(&rmu); return; }
  events_logged++;
  struct buf b; bhead(&b, event);
  bput(&b, ",\"call\":"); bstr(&b, call);
  bput(&b, ",\"path\":"); bstr(&b, path);
  emit(&b);
  pthread_mutex_unlock(&rmu);
}

static void write_exit(const char *how) {
  pthread_mutex_lock(&rmu);
  if (exit_written || load_ok == 0) { pthread_mutex_unlock(&rmu); return; }
  exit_written = 1;
  struct buf b; bhead(&b, "exit");
  bput(&b, ",\"how\":"); bstr(&b, how);
  bput(&b, ",\"counters\":{\"mapped\":"); bnum(&b, c_mapped);
  bput(&b, ",\"frozenDirect\":"); bnum(&b, c_frozen_direct);
  bput(&b, ",\"absent\":"); bnum(&b, c_absent);
  bput(&b, ",\"unmapped\":"); bnum(&b, c_unmapped);
  bput(&b, ",\"writeDenied\":"); bnum(&b, c_write_denied);
  bput(&b, ",\"direntPatched\":"); bnum(&b, c_dirent);
  bput(&b, ",\"fdPatched\":"); bnum(&b, c_fd_patched);
  bput(&b, ",\"reverseMapped\":"); bnum(&b, c_reverse);
  bput(&b, ",\"execs\":"); bnum(&b, c_execs);
  bput(&b, ",\"spawns\":"); bnum(&b, c_spawns);
  bput(&b, ",\"reinjected\":"); bnum(&b, c_reinjected);
  bput(&b, ",\"reaped\":"); bnum(&b, c_reaped);
  bput(&b, ",\"signalsSent\":"); bnum(&b, c_signals);
  bput(&b, ",\"linkTargetAnswered\":"); bnum(&b, c_target);
  bput(&b, ",\"traversals\":"); bnum(&b, c_traversal);
  bput(&b, ",\"probeAnswered\":"); bnum(&b, c_probe);
  bput(&b, ",\"eventsDropped\":"); bnum(&b, c_events_dropped);
  bput(&b, "}");
  emit(&b);
  pthread_mutex_unlock(&rmu);
}

static void write_activation(const char *image) {
  struct buf b; bhead(&b, "activate");
  bput(&b, ",\"schema\":\"rootmap/1\",\"image\":"); bstr(&b, image);
  bput(&b, ",\"ppid\":"); bnum(&b, (unsigned long long)getppid());
  char exe[PROC_PIDPATHINFO_MAXSIZE]; exe[0] = 0;
  proc_pidpath(getpid(), exe, sizeof exe);
  bput(&b, ",\"exe\":"); bstr(&b, exe);
  bput(&b, ",\"library\":"); bstr(&b, self_path);
  bput(&b, ",\"manifest\":"); bstr(&b, manifest_path);
  bput(&b, ",\"manifestSha256\":"); bstr(&b, manifest_sha);
  bput(&b, ",\"ok\":"); bput(&b, load_ok == 1 ? "true" : "false");
  bput(&b, ",\"error\":"); if (load_error[0]) bstr(&b, load_error); else bput(&b, "null");
  bput(&b, ",\"roots\":"); bnum(&b, (unsigned long long)nroots);
  bput(&b, ",\"entries\":"); bnum(&b, (unsigned long long)nents);
  // Independent enforcement: the sandbox must deny every live logical root
  // and every denied link target. The list outgrows one buffer (a freeze
  // holds one file root per out-of-root link target, Amendment 10), so the
  // line is assembled in a mapping sized for it and written with ONE write
  // (Amendment 19 HR-10): a reader never sees a torn activation line. If
  // the mapping fails, the line closes with an empty list and an error, which
  // the family judge refuses (absent enforcement): fail closed, still whole.
  bput(&b, ",\"enforced\":[");
  if (ensure_rfd() < 0) return;
  size_t cap = b.n + 128;
  for (int i = 0; i < nroots + ntargets; i++) {
    const char *p = i < nroots ? roots[i].logical : targets[i - nroots].path;
    cap += 6 * strlen(p) + 64;
  }
  char *line = mmap(NULL, cap, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
  const char *out = b.s;
  size_t n = b.n;
  if (line == MAP_FAILED) {
    bput(&b, "],\"activationError\":\"no buffer for the enforcement list\"}\n");
    out = b.s; n = b.n;
  } else {
    memcpy(line, b.s, b.n); n = b.n;
    int no_report = &SANDBOX_CHECK_NO_REPORT ? SANDBOX_CHECK_NO_REPORT : 0x40000000;
    for (int i = 0; i < nroots + ntargets; i++) {
      const char *p = i < nroots ? roots[i].logical : targets[i - nroots].path;
      int d = sandbox_check(getpid(), "file-read-data", 1 /* SANDBOX_FILTER_PATH */ | no_report, p) > 0;
      b.n = 0; b.s[0] = 0;
      if (i) bput(&b, ",");
      bput(&b, "{\"root\":"); bstr(&b, p);
      if (i >= nroots) bput(&b, ",\"denyOnly\":true");
      bput(&b, ",\"denied\":"); bput(&b, d ? "true" : "false"); bput(&b, "}");
      if (n + b.n + 64 > cap) {            // cannot happen (cap is sized for every
        static const char cut[] = ",{\"root\":\"(truncated)\",\"denied\":false}";
        memcpy(line + n, cut, sizeof cut - 1); n += sizeof cut - 1;   // entry); fail closed
        break;
      }
      memcpy(line + n, b.s, b.n); n += b.n;
    }
    memcpy(line + n, "]}\n", 3); n += 3;
    out = line;
  }
  raw_write(rfd, out, n);
  if (line != MAP_FAILED) munmap(line, cap);
}

// ── manifest load ────────────────────────────────────────────────────────
static unsigned long hpath(const char *s) { unsigned long h = 1469598103934665603UL; while (*s) { h ^= (unsigned char)*s++; h *= 1099511628211UL; } return h; }

static struct ent *lookup(const char *p) {
  if (!buckets) return NULL;
  for (int i = buckets[hpath(p) & (unsigned long)(nbuckets - 1)]; i >= 0; i = ents[i].next)
    if (strcmp(ents[i].path, p) == 0) return &ents[i];
  return NULL;
}

static int split(char *line, char **f, int max) {
  int n = 0; f[n++] = line;
  for (char *c = line; *c; c++) if (*c == '\t') { *c = 0; if (n < max) f[n++] = c + 1; else return -1; }
  return n;
}

// The 16 META fields (see the manifest notes) into one struct meta.
static void read_meta(char **f, struct meta *m) {
  unsigned long long v[16];
  for (int i = 0; i < 16; i++) v[i] = strtoull(f[i], NULL, 10);
  m->dev = v[0]; m->ino = v[1]; m->size = v[2]; m->mode = v[3]; m->nlink = v[4]; m->uid = v[5]; m->gid = v[6];
  m->at = (struct timespec){(time_t)v[7], (long)v[8]}; m->mt = (struct timespec){(time_t)v[9], (long)v[10]};
  m->ct = (struct timespec){(time_t)v[11], (long)v[12]}; m->bt = (struct timespec){(time_t)v[13], (long)v[14]};
}

static void fail_load(const char *why) {
  load_ok = -1; snprintf(load_error, sizeof load_error, "%s", why);
}

static void load_manifest(void) {
  const char *m = getenv("ROOTMAP_MANIFEST");
  const char *r = getenv("ROOTMAP_RECEIPTS");
  if (r) { snprintf(receipts_dir, sizeof receipts_dir, "%s", r); snprintf(env_receipts, sizeof env_receipts, "ROOTMAP_RECEIPTS=%s", r); }
  if (!m) return;                         // not configured: inert
  snprintf(manifest_path, sizeof manifest_path, "%s", m);
  snprintf(env_manifest, sizeof env_manifest, "ROOTMAP_MANIFEST=%s", m);
  int fd = open(m, O_RDONLY | O_CLOEXEC);
  if (fd < 0) { fail_load("manifest unreadable"); return; }
  struct stat st;
  if (fstat(fd, &st) != 0 || st.st_size <= 0) { close(fd); fail_load("manifest empty"); return; }
  char *data = malloc((size_t)st.st_size + 1);
  if (!data) { close(fd); fail_load("out of memory"); return; }
  size_t got = 0;
  while (got < (size_t)st.st_size) { ssize_t n = read(fd, data + got, (size_t)st.st_size - got); if (n <= 0) break; got += (size_t)n; }
  close(fd);
  if (got != (size_t)st.st_size) { fail_load("manifest short read"); return; }
  data[got] = 0;
  unsigned char md[CC_SHA256_DIGEST_LENGTH];
  CC_SHA256(data, (CC_LONG)got, md);
  for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) snprintf(manifest_sha + 2 * i, 3, "%02x", md[i]);
  int lines = 0, rlines = 0, xlines = 0;
  for (size_t i = 0; i < got; i++) {
    if (i == 0 || data[i - 1] == '\n') { if (data[i] == 'R') rlines++; else if (data[i] == 'X') xlines++; }
    if (data[i] == '\n') lines++;
  }
  ents = calloc((size_t)lines + 1, sizeof *ents);
  roots = calloc((size_t)rlines + 1, sizeof *roots);
  targets = calloc((size_t)xlines + 1, sizeof *targets);
  nbuckets = 1; while (nbuckets < 2 * (lines + 1)) nbuckets <<= 1;
  buckets = malloc(sizeof(int) * (size_t)nbuckets);
  if (!ents || !roots || !targets || !buckets) { fail_load("out of memory"); return; }
  for (int i = 0; i < nbuckets; i++) buckets[i] = -1;
  char *save = NULL; int first = 1;
  for (char *line = strtok_r(data, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
    char *f[24]; int n = split(line, f, 24);
    if (first) { first = 0; if (n != 2 || strcmp(f[0], "ROOTMAP") || strcmp(f[1], "2")) { fail_load("bad manifest header (recapture the freeze)"); return; } continue; }
    if (n < 2) { fail_load("malformed manifest line"); return; }
    if (f[0][0] == 'R' && n == 5) {
      if (nroots >= rlines) { fail_load("malformed root record"); return; }
      struct root *x = &roots[nroots++];
      x->logical = f[1]; x->llen = strlen(f[1]); x->frozen = f[2]; x->flen = strlen(f[2]);
      x->is_file = atoi(f[3]); x->present = atoi(f[4]);
      if (x->present && realpath_plain(x->frozen, x->canon)) x->clen = strlen(x->canon);
      else { snprintf(x->canon, sizeof x->canon, "%s", x->frozen); x->clen = x->flen; }
      struct stat fs;
      if (x->present && !have_frozen_dev && stat(x->frozen, &fs) == 0) { frozen_dev = fs.st_dev; have_frozen_dev = 1; }
    } else if (f[0][0] == 'E' && (n == 19 || n == 21) && f[1][0] && !f[1][1] && strchr("FDLP", f[1][0])) {
      struct ent *e = &ents[nents];
      e->kind = f[1][0]; e->path = f[2];
      read_meta(f + 3, &e->m);
      if (n == 21) { e->target = f[19]; e->resolved = f[20]; }
      unsigned long h = hpath(e->path) & (unsigned long)(nbuckets - 1);
      e->next = buckets[h]; buckets[h] = nents++;
    } else if (f[0][0] == 'S' && n == 18) {
      struct ent *e = lookup(f[1]);
      if (!e || e->kind != 'P' || !e->target) { fail_load("S record without its probe link"); return; }
      e->has_fo = 1; read_meta(f + 2, &e->fo);
    } else if (f[0][0] == 'A' && n == 2) {
      struct ent *e = &ents[nents];
      e->kind = 'A'; e->path = f[1];
      unsigned long h = hpath(e->path) & (unsigned long)(nbuckets - 1);
      e->next = buckets[h]; buckets[h] = nents++;
    } else if (f[0][0] == 'X' && (n == 2 || n == 18) && ntargets < xlines) {
      struct target *t = &targets[ntargets++];
      t->path = f[1]; t->len = strlen(f[1]);
      if (n == 18) { t->has_meta = 1; read_meta(f + 2, &t->m); }
    } else { fail_load("malformed manifest record"); return; }
  }
  if (!nroots) { fail_load("manifest names no root"); return; }
  // An L entry whose resolved target is an X path is a non-traversed
  // directory link (a covered file link's target is a file root, never X).
  for (int i = 0; i < nents; i++) {
    if (ents[i].kind != 'L' || !ents[i].resolved) continue;
    for (int j = 0; j < ntargets; j++)
      if (strcmp(ents[i].resolved, targets[j].path) == 0) { ents[i].dirlink = 1; ents[i].tgt = j; break; }
  }
  load_ok = 1;
}

// ── path classification ──────────────────────────────────────────────────
// TARGET: the live target of a non-traversed directory link (an X record)
// outside every root, or a path under it (r indexes `targets`).
enum cls { PASS = 0, MAPPED = 1, FROZEN = 2, TARGET = 3 };
struct res { int c; int r; char logical[MAXPATHLEN]; char frozen[MAXPATHLEN]; };

// Lexical normalization of an absolute path: "//", "." and ".." (the freeze
// refuses directory symlinks inside a root, so ".." is lexical there).
static int normalize(const char *in, char *out, size_t cap) {
  size_t n = 0; out[0] = 0;
  const char *p = in;
  while (*p) {
    while (*p == '/') p++;
    if (!*p) break;
    const char *s = p; while (*p && *p != '/') p++;
    size_t len = (size_t)(p - s);
    if (len == 1 && s[0] == '.') continue;
    if (len == 2 && s[0] == '.' && s[1] == '.') { while (n > 0 && out[n - 1] != '/') n--; if (n > 0) n--; out[n] = 0; continue; }
    if (n + 1 + len + 1 > cap) return -1;
    out[n++] = '/'; memcpy(out + n, s, len); n += len; out[n] = 0;
  }
  if (n == 0) { if (cap < 2) return -1; out[0] = '/'; out[1] = 0; }
  return 0;
}

static int under(const char *path, const char *pre, size_t plen, int exact_only) {
  if (strncmp(path, pre, plen) != 0) return 0;
  if (path[plen] == 0) return 1;
  return !exact_only && path[plen] == '/';
}

// libc's getcwd opens "." (open$NOCANCEL) when the kernel's fast path
// fails, so resolving a relative path re-enters classify(): the guard makes
// that inner open pass through instead of recursing.
static __thread int in_base;

static int classify(int dirfd, const char *path, struct res *out) {
  out->c = PASS;
  if (load_ok != 1 || !path || !*path) return PASS;
  char abs[2 * MAXPATHLEN];
  if (path[0] == '/') {
    if (strlen(path) >= sizeof abs) return PASS;
    strcpy(abs, path);
  } else {
    if (in_base) return PASS;
    char base[MAXPATHLEN];
    in_base = 1;
    int ok = dirfd == AT_FDCWD ? getcwd(base, sizeof base) != NULL
                               : fcntl(dirfd, F_GETPATH, base) != -1;
    in_base = 0;
    if (!ok) return PASS;
    if (snprintf(abs, sizeof abs, "%s/%s", base, path) >= (int)sizeof abs) return PASS;
  }
  char norm[MAXPATHLEN];
  if (normalize(abs, norm, sizeof norm) != 0) return PASS;
  int best = -1; size_t bestlen = 0; int frozen = 0;
  for (int i = 0; i < nroots; i++) {
    struct root *x = &roots[i];
    if (under(norm, x->logical, x->llen, x->is_file) && x->llen > bestlen) { best = i; bestlen = x->llen; frozen = 0; }
    if (under(norm, x->frozen, x->flen, x->is_file) && x->flen > bestlen) { best = i; bestlen = x->flen; frozen = 1; }
    if (x->clen != x->flen || strcmp(x->canon, x->frozen))
      if (under(norm, x->canon, x->clen, x->is_file) && x->clen > bestlen) { best = i; bestlen = x->clen; frozen = 2; }
  }
  if (best < 0) {
    for (int i = 0; i < ntargets; i++)
      if (under(norm, targets[i].path, targets[i].len, 0)) {
        snprintf(out->logical, sizeof out->logical, "%s", norm);
        out->frozen[0] = 0;
        out->r = i;
        return out->c = TARGET;
      }
    return PASS;
  }
  struct root *x = &roots[best];
  const char *suffix = norm + (frozen == 0 ? x->llen : frozen == 1 ? x->flen : x->clen);
  if (snprintf(out->logical, sizeof out->logical, "%s%s", x->logical, suffix) >= (int)sizeof out->logical) return PASS;
  if (snprintf(out->frozen, sizeof out->frozen, "%s%s", x->frozen, suffix) >= (int)sizeof out->frozen) return PASS;
  out->r = best;
  out->c = frozen ? FROZEN : MAPPED;
  return out->c;
}

// Under a recorded absence (the path itself or an ancestor at or below
// `floor`, the length of its root's or its denied target's spelling)?
static int absent_from(const char *logical, size_t floor) {
  char tmp[MAXPATHLEN]; snprintf(tmp, sizeof tmp, "%s", logical);
  for (;;) {
    struct ent *e = lookup(tmp);
    if (e && e->kind == 'A') return 1;
    char *slash = strrchr(tmp, '/');
    if (!slash || (size_t)(slash - tmp) < floor) return 0;
    *slash = 0;
  }
}
static int absent(const char *logical, int r) { return absent_from(logical, roots[r].llen); }

// Below a non-traversed directory link (an ancestor inside its root)?
static int under_dirlink(const char *logical, int r) {
  char tmp[MAXPATHLEN]; snprintf(tmp, sizeof tmp, "%s", logical);
  size_t floor = roots[r].llen;
  for (;;) {
    char *slash = strrchr(tmp, '/');
    if (!slash || (size_t)(slash - tmp) < floor) return 0;
    *slash = 0;
    struct ent *e = lookup(tmp);
    if (e && e->kind == 'L' && e->dirlink) return 1;
  }
}

// A refused access of a non-traversed link's live target, of anything under
// it or under the link (Amendment 13 O1): no kernel call, INVALID.
static int traversal(const char *call, const char *path) {
  c_traversal++; event_path("traversal", call, path); errno = EACCES; return -1;
}

// The gate every mapped call passes. 0 = proceed on res->frozen, or - inside
// a denied target, where there is no frozen path - on the probe entry *eo.
static int gate(struct res *x, const char *call, int write, struct ent **eo) {
  if (eo) *eo = NULL;
  if (write) { c_write_denied++; event_path("write-denied", call, x->logical); errno = EACCES; return -1; }
  if (x->c == TARGET) {
    // Amendment 14 P1: inside a denied target, a probe entry (the git-root
    // walk-up from a recorded cwd at or below the target) is answered as O2
    // answers one, and a path at or under a probe absence is ENOENT; every
    // other access stays a traversal. The target itself never has an entry,
    // so its own O1 presentation is decided by the callers before the gate.
    struct ent *e = lookup(x->logical);
    if (e && e->kind == 'P') { if (eo) *eo = e; return 0; }
    if (absent_from(x->logical, targets[x->r].len)) { c_absent++; errno = ENOENT; return -1; }
    return traversal(call, x->logical);
  }
  struct ent *e = lookup(x->logical);
  if (e && e->kind != 'A') { if (x->c == FROZEN) c_frozen_direct++; else c_mapped++; if (eo) *eo = e; return 0; }
  if ((e && e->kind == 'A') || absent(x->logical, x->r) || !roots[x->r].present) { c_absent++; errno = ENOENT; return -1; }
  if (under_dirlink(x->logical, x->r)) return traversal(call, x->logical);
  c_unmapped++; event_path("unmapped", call, x->logical); errno = EACCES; return -1;
}

static void patch_meta(struct stat *st, const struct meta *m) {
  st->st_dev = (dev_t)m->dev; st->st_ino = (ino_t)m->ino; st->st_size = (off_t)m->size;
  st->st_mode = (mode_t)m->mode; st->st_nlink = (nlink_t)m->nlink;
  st->st_uid = (uid_t)m->uid; st->st_gid = (gid_t)m->gid;
  st->st_atimespec = m->at; st->st_mtimespec = m->mt; st->st_ctimespec = m->ct; st->st_birthtimespec = m->bt;
}
static void patch(struct stat *st, const struct ent *e) { patch_meta(st, &e->m); }

// A stat answered from captured metadata alone: no kernel call at all.
static void fill(struct stat *st, const struct meta *m) {
  memset(st, 0, sizeof *st);
  st->st_blksize = 4096;
  patch_meta(st, m);
}

// The metadata a call on the live target itself, exactly, presents (NULL:
// below it, or a target captured without metadata - refuse).
static const struct meta *target_meta(const struct res *x) {
  const struct target *t = &targets[x->r];
  return t->has_meta && strcmp(x->logical, t->path) == 0 ? &t->m : NULL;
}
// ... and what a FOLLOWING call through a non-traversed link presents.
static const struct meta *dirlink_meta(const struct ent *e) {
  return e->dirlink && targets[e->tgt].has_meta ? &targets[e->tgt].m : NULL;
}

// A refused open, listing or chdir of a probe entry (Amendment 13 O2): it is
// metadata only, there are no bytes to read; INVALID.
static int probe_refused(const char *call, const char *path) {
  c_unmapped++; event_path("unmapped-open", call, path); errno = EACCES; return -1;
}

// What a stat of a probe entry presents (NULL: a dangling probe link followed).
static const struct meta *probe_meta(const struct ent *e, int follow) {
  if (follow && e->target) return e->has_fo ? &e->fo : NULL;
  return &e->m;
}

// The logical spelling a realpath returns without touching anything.
static char *answer_path(const char *p, char *resolved) {
  if (resolved) { if (strlen(p) >= MAXPATHLEN) { errno = ENAMETOOLONG; return NULL; } strcpy(resolved, p); return resolved; }
  char *d = strdup(p);
  if (!d) errno = ENOMEM;
  return d;
}

// The identity a stat that FOLLOWS links reports for this entry.
static const struct ent *followed(const struct ent *e, const char *call) {
  if (e->kind != 'L') return e;
  struct ent *t = lookup(e->resolved);
  if (!t || t->kind == 'A') { c_unmapped++; event_path("unmapped", call, e->resolved); return NULL; }
  return t;
}

// Rewrite a frozen path in `p` (capacity cap) to its logical spelling.
static void reverse(char *p, size_t cap) {
  struct res x;
  if (load_ok != 1 || !p || p[0] != '/') return;
  if (classify(AT_FDCWD, p, &x) != FROZEN) return;
  if (strlen(x.logical) + 1 > cap) return;
  strcpy(p, x.logical);
  c_reverse++;
}

static int is_write_flags(int flags) {
  return (flags & O_ACCMODE) != O_RDONLY || (flags & (O_CREAT | O_TRUNC | O_APPEND));
}

// ── interposers ──────────────────────────────────────────────────────────
static int open_common(int dirfd, const char *path, int flags, int mode, int nocancel) {
  struct res x; struct ent *e;
  if (classify(dirfd, path, &x) != PASS) {
    if (gate(&x, "open", is_write_flags(flags), &e) < 0) return -1;
    if (e->kind == 'P') return probe_refused("open", x.logical);
    // A non-traversed directory link: only an open of the link itself
    // (O_SYMLINK, or O_NOFOLLOW's ELOOP) never follows it to the live target.
    if (e->dirlink && !(flags & (O_SYMLINK | O_NOFOLLOW))) return traversal("open", x.logical);
    return nocancel ? open_nocancel_v(x.frozen, flags, mode) : open(x.frozen, flags, mode);
  }
  if (dirfd == AT_FDCWD) return nocancel ? open_nocancel_v(path, flags, mode) : open(path, flags, mode);
  return nocancel ? openat_nocancel_v(dirfd, path, flags, mode) : openat(dirfd, path, flags, mode);
}

int rm_open(const char *path, int flags, ...) {
  int mode = 0; if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap); }
  return open_common(AT_FDCWD, path, flags, mode, 0);
}
int rm_open_nc(const char *path, int flags, ...) {
  int mode = 0; if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap); }
  return open_common(AT_FDCWD, path, flags, mode, 1);
}
int rm_openat(int dirfd, const char *path, int flags, ...) {
  int mode = 0; if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap); }
  return open_common(dirfd, path, flags, mode, 0);
}
int rm_openat_nc(int dirfd, const char *path, int flags, ...) {
  int mode = 0; if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap); }
  return open_common(dirfd, path, flags, mode, 1);
}

static int stat_common(int dirfd, const char *path, struct stat *st, int follow, const char *call) {
  struct res x; struct ent *e;
  int c = classify(dirfd, path, &x);
  if (c != PASS) {
    if (c == TARGET) {                       // the live target itself: metadata only
      const struct meta *m = target_meta(&x);
      if (m) { fill(st, m); c_target++; return 0; }
    }                                        // below it: a probe, an absence or a traversal
    if (gate(&x, call, 0, &e) < 0) return -1;
    if (e->kind == 'P') {                    // a probe: its metadata, no kernel call
      const struct meta *m = probe_meta(e, follow);
      if (!m) { errno = ENOENT; return -1; }
      fill(st, m); c_probe++; return 0;
    }
    if (e->dirlink && follow) {              // through the link: the target's metadata
      const struct meta *m = dirlink_meta(e);
      if (!m) return traversal(call, x.logical);
      fill(st, m); c_target++; return 0;
    }
    int r = follow ? stat(x.frozen, st) : lstat(x.frozen, st);
    if (r == 0) {
      const struct ent *id = follow ? followed(e, call) : e;
      if (!id) { errno = EACCES; return -1; }
      patch(st, id);
    }
    return r;
  }
  if (dirfd == AT_FDCWD) return follow ? stat(path, st) : lstat(path, st);
  return fstatat(dirfd, path, st, follow ? 0 : AT_SYMLINK_NOFOLLOW);
}
int rm_stat(const char *p, struct stat *st) { return stat_common(AT_FDCWD, p, st, 1, "stat"); }
int rm_lstat(const char *p, struct stat *st) { return stat_common(AT_FDCWD, p, st, 0, "lstat"); }
int rm_fstatat(int dirfd, const char *p, struct stat *st, int flag) {
  return stat_common(dirfd, p, st, !(flag & AT_SYMLINK_NOFOLLOW), "fstatat");
}

int rm_fstat(int fd, struct stat *st) {
  int r = fstat(fd, st);
  if (r != 0 || load_ok != 1 || !have_frozen_dev || st->st_dev != frozen_dev) return r;
  char p[MAXPATHLEN]; struct res x;
  if (fcntl(fd, F_GETPATH, p) == -1 || classify(AT_FDCWD, p, &x) != FROZEN) return r;
  struct ent *e = lookup(x.logical);
  if (!e || e->kind == 'A') { c_unmapped++; event_path("unmapped-fd", "fstat", x.logical); errno = EACCES; return -1; }
  patch(st, e); c_fd_patched++;
  return 0;
}

static int access_common(int dirfd, const char *path, int mode, int flag, const char *call) {
  struct res x; struct ent *e;
  int c = classify(dirfd, path, &x), w = (mode & W_OK) != 0;
  if (c != PASS) {
    if (c == TARGET && !w && target_meta(&x)) {   // the live target itself: it exists
      c_target++; return 0;
    }                                        // below it: a probe, an absence or a traversal
    if (gate(&x, call, w, &e) < 0) return -1;
    if (e->kind == 'P') {
      if (!probe_meta(e, !(flag & AT_SYMLINK_NOFOLLOW))) { errno = ENOENT; return -1; }
      c_probe++; return 0;
    }
    if (e->dirlink && !(flag & AT_SYMLINK_NOFOLLOW)) {
      if (!dirlink_meta(e)) return traversal(call, x.logical);
      c_target++; return 0;
    }
    return faccessat(AT_FDCWD, x.frozen, mode, flag);
  }
  return dirfd == AT_FDCWD && !flag ? access(path, mode) : faccessat(dirfd, path, mode, flag);
}
int rm_access(const char *p, int mode) { return access_common(AT_FDCWD, p, mode, 0, "access"); }
int rm_faccessat(int dirfd, const char *p, int mode, int flag) { return access_common(dirfd, p, mode, flag, "faccessat"); }

static ssize_t readlink_common(int dirfd, const char *path, char *buf, size_t sz, const char *call) {
  struct res x; struct ent *e;
  if (classify(dirfd, path, &x) != PASS) {
    if (gate(&x, call, 0, &e) < 0) return -1;
    if (e->kind == 'P') {                    // a probe link's stored target
      if (!e->target) { errno = EINVAL; return -1; }
      size_t n = strlen(e->target); if (n > sz) n = sz;
      memcpy(buf, e->target, n); c_probe++; return (ssize_t)n;
    }
    // The LOGICAL spelling of a link reads its stored target. The frozen
    // spelling reads the frozen link itself (a relative link to the frozen
    // copy): libc's realpath of a redirected path resolves through it, and
    // must stay inside the freeze rather than follow an out-of-root target's
    // live path with calls this library does not see (Amendment 10).
    if (e->kind == 'L' && x.c == MAPPED) {
      size_t n = strlen(e->target); if (n > sz) n = sz;
      memcpy(buf, e->target, n); return (ssize_t)n;
    }
    return readlink(x.frozen, buf, sz);
  }
  return dirfd == AT_FDCWD ? readlink(path, buf, sz) : readlinkat(dirfd, path, buf, sz);
}
ssize_t rm_readlink(const char *p, char *b, size_t n) { return readlink_common(AT_FDCWD, p, b, n, "readlink"); }
ssize_t rm_readlinkat(int d, const char *p, char *b, size_t n) { return readlink_common(d, p, b, n, "readlinkat"); }

static char *realpath_common(const char *path, char *resolved, int ext) {
  struct res x;
  char *(*real)(const char *, char *) = ext ? realpath_ext : realpath_plain;
  int c = classify(AT_FDCWD, path, &x);
  if (c == PASS) return real(path, resolved);
  if (c == TARGET && target_meta(&x)) {      // the live target itself: canonical
    c_target++; return answer_path(x.logical, resolved);
  }                                          // below it: a probe, an absence or a traversal
  struct ent *e;
  if (gate(&x, "realpath", 0, &e) < 0) return NULL;
  if (e->dirlink) { c_target++; return answer_path(e->resolved, resolved); }   // untouched
  if (e->kind == 'P') {                      // canonical, or its link's resolution
    if (e->target && !e->resolved[0]) { errno = ENOENT; return NULL; }
    c_probe++; return answer_path(e->target ? e->resolved : x.logical, resolved);
  }
  char *r = real(x.frozen, resolved);
  if (!r) return NULL;
  struct res y;
  if (classify(AT_FDCWD, r, &y) == FROZEN) {
    if (resolved) { if (strlen(y.logical) < MAXPATHLEN) { strcpy(r, y.logical); c_reverse++; } }
    else { char *d = strdup(y.logical); if (d) { free(r); r = d; c_reverse++; } }
  }
  return r;
}
char *rm_realpath(const char *p, char *r) { return realpath_common(p, r, 0); }
char *rm_realpath_ext(const char *p, char *r) { return realpath_common(p, r, 1); }

DIR *rm_opendir(const char *path) {
  struct res x; struct ent *e;
  if (classify(AT_FDCWD, path, &x) != PASS) {
    if (gate(&x, "opendir", 0, &e) < 0) return NULL;
    if (e->dirlink) { traversal("opendir", x.logical); return NULL; }
    if (e->kind == 'P') { probe_refused("opendir", x.logical); return NULL; }
    return opendir(x.frozen);
  }
  return opendir(path);
}
DIR *rm_opendir2(const char *path, int flags) {
  struct res x; struct ent *e;
  if (classify(AT_FDCWD, path, &x) != PASS) {
    if (gate(&x, "__opendir2", 0, &e) < 0) return NULL;
    if (e->dirlink) { traversal("__opendir2", x.logical); return NULL; }
    if (e->kind == 'P') { probe_refused("__opendir2", x.logical); return NULL; }
    return __opendir2(x.frozen, flags);
  }
  return __opendir2(path, flags);
}
DIR *rm_fdopendir(int fd) { return fdopendir(fd); }   // the fd already names the freeze

// Patch a directory entry read from a frozen directory with the captured inode.
static void patch_dirent(DIR *d, struct dirent *ent) {
  if (!ent || load_ok != 1) return;
  char p[MAXPATHLEN]; struct res x;
  if (fcntl(dirfd(d), F_GETPATH, p) == -1 || classify(AT_FDCWD, p, &x) != FROZEN) return;
  if (strcmp(ent->d_name, ".") == 0) { struct ent *e = lookup(x.logical); if (e) { ent->d_ino = e->m.ino; c_dirent++; } return; }
  if (strcmp(ent->d_name, "..") == 0) {
    char parent[MAXPATHLEN]; snprintf(parent, sizeof parent, "%s", x.logical);
    char *s = strrchr(parent, '/'); if (s && s != parent) *s = 0; else strcpy(parent, "/");
    struct ent *e = lookup(parent);
    struct stat st;
    if (e) { ent->d_ino = e->m.ino; c_dirent++; }
    else if (stat(parent, &st) == 0) { ent->d_ino = st.st_ino; c_dirent++; }
    return;
  }
  char child[MAXPATHLEN];
  if (snprintf(child, sizeof child, "%s/%s", x.logical, ent->d_name) >= (int)sizeof child) return;
  struct ent *e = lookup(child);
  if (!e || e->kind == 'A') { c_unmapped++; event_path("unmapped-dirent", "readdir", child); return; }
  ent->d_ino = e->m.ino; c_dirent++;
}
struct dirent *rm_readdir(DIR *d) { struct dirent *e = readdir(d); patch_dirent(d, e); return e; }
int rm_readdir_r(DIR *d, struct dirent *entry, struct dirent **result) {
  int r = readdir_r(d, entry, result);
  if (r == 0 && result && *result) patch_dirent(d, *result);
  return r;
}

int rm_fcntl(int fd, int cmd, ...) {
  va_list ap; va_start(ap, cmd); intptr_t arg = va_arg(ap, intptr_t); va_end(ap);
  int r = fcntl(fd, cmd, arg);
  if (r != -1 && (cmd == F_GETPATH || cmd == F_GETPATH_NOFIRMLINK)) reverse((char *)arg, MAXPATHLEN);
  return r;
}

int rm_chdir(const char *path) {
  struct res x; struct ent *e;
  if (classify(AT_FDCWD, path, &x) != PASS) {
    if (gate(&x, "chdir", 0, &e) < 0) return -1;
    if (e->dirlink) return traversal("chdir", x.logical);
    if (e->kind == 'P') return probe_refused("chdir", x.logical);
    return chdir(x.frozen);
  }
  return chdir(path);
}

char *rm_getcwd(char *buf, size_t size) {
  char *r = getcwd(buf, size);
  if (!r || load_ok != 1) return r;
  struct res x;
  if (classify(AT_FDCWD, r, &x) != FROZEN) return r;
  size_t need = strlen(x.logical) + 1;
  if (buf) { if (need > size) { errno = ERANGE; return NULL; } strcpy(buf, x.logical); c_reverse++; return buf; }
  char *d = strdup(x.logical);
  if (!d) return r;
  free(r); c_reverse++;
  return d;
}

// ── children: re-inject a namespace the environment lost ─────────────────
#define MAXENV 2048
// Builds `out` (stack storage owned by the caller) from envp, adding this
// library to DYLD_INSERT_LIBRARIES and the manifest/receipt variables when
// missing. Returns 1 when anything was added, 0 when nothing was needed, -1
// when the environment is too large (the child then starts as given).
static int compose_env(char *const envp[], char **out, char *dyld, size_t dcap) {
  if (load_ok != 1 || !self_path[0]) return 0;
  int n = 0, have_m = 0, have_r = 0, dyld_at = -1, added = 0;
  for (char *const *e = envp; e && *e; e++) {
    if (n >= MAXENV - 4) return -1;
    if (strncmp(*e, "DYLD_INSERT_LIBRARIES=", 22) == 0) dyld_at = n;
    if (strncmp(*e, "ROOTMAP_MANIFEST=", 17) == 0) have_m = 1;
    if (strncmp(*e, "ROOTMAP_RECEIPTS=", 17) == 0) have_r = 1;
    out[n++] = *e;
  }
  if (dyld_at < 0 || !strstr(out[dyld_at] + 22, self_path)) {
    const char *old = dyld_at >= 0 ? out[dyld_at] + 22 : "";
    if (snprintf(dyld, dcap, "DYLD_INSERT_LIBRARIES=%s%s%s", self_path, *old ? ":" : "", old) >= (int)dcap) return -1;
    if (dyld_at >= 0) out[dyld_at] = dyld; else out[n++] = dyld;
    added = 1;
  }
  if (!have_m && env_manifest[0]) { out[n++] = env_manifest; added = 1; }
  if (!have_r && env_receipts[0]) { out[n++] = env_receipts; added = 1; }
  out[n] = NULL;
  return added;
}

// The interpreter of a "#!" script (the kernel execs it instead of the
// target, so a system interpreter such as /usr/bin/env strips DYLD_*).
static void shebang(const char *path, char *out, size_t cap) {
  out[0] = 0;
  if (!path) return;
  int fd = open(path, O_RDONLY | O_CLOEXEC);
  if (fd < 0) return;
  char head[256];
  ssize_t n = read(fd, head, sizeof head - 1);
  close(fd);
  if (n < 3 || head[0] != '#' || head[1] != '!') return;
  head[n] = 0;
  char *p = head + 2;
  while (*p == ' ' || *p == '\t') p++;
  size_t i = 0;
  while (p[i] && p[i] != ' ' && p[i] != '\t' && p[i] != '\n' && i + 1 < cap) { out[i] = p[i]; i++; }
  out[i] = 0;
}

// One launch record (spec §6.3 "Frozen namespace", Q17): the program (and
// its "#!" interpreter), the arguments, the parent (this process's parent for
// an exec, which keeps the pid; this process for a spawn) and the child pid.
static void event_child(const char *event, const char *target, char *const argv[],
                        long child, int reinjected, int err) {
  char interp[MAXPATHLEN];
  shebang(target, interp, sizeof interp);
  pthread_mutex_lock(&rmu);
  struct buf b; bhead(&b, event);
  bput(&b, ",\"target\":"); bstrn(&b, target ? target : "", 1024);
  if (interp[0]) { bput(&b, ",\"interpreter\":"); bstrn(&b, interp, 1024); }
  bput(&b, ",\"ppid\":"); bnum(&b, (unsigned long long)getppid());
  if (child >= 0) { bput(&b, ",\"child\":"); bnum(&b, (unsigned long long)child); }
  bargv(&b, argv);
  bput(&b, ",\"reinjected\":"); bput(&b, reinjected > 0 ? "true" : reinjected < 0 ? "\"overflow\"" : "false");
  if (err) { bput(&b, ",\"errno\":"); bnum(&b, (unsigned long long)err); }
  emit(&b);
  pthread_mutex_unlock(&rmu);
}

int rm_execve(const char *path, char *const argv[], char *const envp[]) {
  char *env[MAXENV]; char dyld[4 * MAXPATHLEN];
  int added = compose_env(envp, env, dyld, sizeof dyld);
  if (added > 0) c_reinjected++;
  c_execs++;
  event_child("exec", path, argv, -1, added, 0);
  int r = execve(path, argv, added > 0 ? env : envp);
  int saved = errno;
  event_child("exec-failed", path, argv, -1, added, saved);
  errno = saved;
  return r;
}

int rm_posix_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *fa,
                   const posix_spawnattr_t *attr, char *const argv[], char *const envp[]) {
  char *env[MAXENV]; char dyld[4 * MAXPATHLEN];
  int added = compose_env(envp, env, dyld, sizeof dyld);
  if (added > 0) c_reinjected++;
  short flags = 0;
  if (attr && posix_spawnattr_getflags(attr, &flags) == 0 && (flags & POSIX_SPAWN_SETEXEC)) {
    // exec semantics (the framework python launcher): logged before, like execve
    c_execs++;
    event_child("exec", path, argv, -1, added, 0);
  }
  pid_t child = -1;
  int r = posix_spawn(&child, path, fa, attr, argv, added > 0 ? env : envp);
  if (pid) *pid = child;
  c_spawns++;
  event_child(r == 0 ? "spawn" : "spawn-failed", path, argv, r == 0 ? (long)child : -1, added, r);
  return r;
}

// ── child termination and signals (Q17) ──────────────────────────────────
// The sandbox kills a violating process with SIGKILL and logs nothing, and a
// parent may swallow the failure; the parent's own wait records the status.
static void note_reaped(pid_t child, int exited, int code, int signaled, int sig,
                        int raw, const char *call) {
  if (load_ok != 1) return;
  c_reaped++;
  pthread_mutex_lock(&rmu);
  struct buf b; bhead(&b, "reaped");
  bput(&b, ",\"call\":"); bstr(&b, call);
  bput(&b, ",\"child\":"); bnum(&b, (unsigned long long)child);
  bput(&b, ",\"status\":"); bnum(&b, (unsigned long long)(unsigned)raw);
  bput(&b, ",\"exited\":"); bput(&b, exited ? "true" : "false");
  bput(&b, ",\"code\":"); if (exited) bnum(&b, (unsigned long long)code); else bput(&b, "null");
  bput(&b, ",\"signaled\":"); bput(&b, signaled ? "true" : "false");
  bput(&b, ",\"signal\":"); if (signaled) bnum(&b, (unsigned long long)sig); else bput(&b, "null");
  emit(&b);
  pthread_mutex_unlock(&rmu);
}
static void note_status(pid_t r, int status, const char *call) {
  if (r <= 0) return;
  if (WIFEXITED(status)) note_reaped(r, 1, WEXITSTATUS(status), 0, 0, status, call);
  else if (WIFSIGNALED(status)) note_reaped(r, 0, 0, 1, WTERMSIG(status), status, call);
}
pid_t rm_waitpid(pid_t pid, int *status, int options) {
  int s = 0; pid_t r = waitpid(pid, &s, options); int saved = errno;
  if (r > 0 && status) *status = s;
  note_status(r, s, "waitpid"); errno = saved; return r;
}
pid_t rm_wait4(pid_t pid, int *status, int options, struct rusage *ru) {
  int s = 0; pid_t r = wait4(pid, &s, options, ru); int saved = errno;
  if (r > 0 && status) *status = s;
  note_status(r, s, "wait4"); errno = saved; return r;
}
pid_t rm_wait3(int *status, int options, struct rusage *ru) {
  int s = 0; pid_t r = wait3(&s, options, ru); int saved = errno;
  if (r > 0 && status) *status = s;
  note_status(r, s, "wait3"); errno = saved; return r;
}
pid_t rm_wait(int *status) {
  int s = 0; pid_t r = wait(&s); int saved = errno;
  if (r > 0 && status) *status = s;
  note_status(r, s, "wait"); errno = saved; return r;
}
int rm_waitid(idtype_t idtype, id_t id, siginfo_t *info, int options) {
  int r = waitid(idtype, id, info, options); int saved = errno;
  if (r == 0 && info && info->si_pid > 0) {
    if (info->si_code == CLD_EXITED)
      note_reaped(info->si_pid, 1, info->si_status, 0, 0, info->si_status, "waitid");
    else if (info->si_code == CLD_KILLED || info->si_code == CLD_DUMPED)
      note_reaped(info->si_pid, 0, 0, 1, info->si_status, info->si_status, "waitid");
  }
  errno = saved; return r;
}
static void note_signal(const char *call, long target, int sig) {
  if (load_ok != 1 || sig == 0) return;
  c_signals++;
  pthread_mutex_lock(&rmu);
  struct buf b; bhead(&b, "signal-sent");
  bput(&b, ",\"call\":"); bstr(&b, call);
  bput(&b, ",\"target\":"); if (target < 0) { bput(&b, "-"); bnum(&b, (unsigned long long)(-target)); } else bnum(&b, (unsigned long long)target);
  bput(&b, ",\"signal\":"); bnum(&b, (unsigned long long)sig);
  emit(&b);
  pthread_mutex_unlock(&rmu);
}
int rm_kill(pid_t pid, int sig) { note_signal("kill", (long)pid, sig); return kill(pid, sig); }
int rm_killpg(pid_t pgrp, int sig) { note_signal("killpg", (long)pgrp, sig); return killpg(pgrp, sig); }

void rm__exit(int status) { write_exit("_exit"); _exit(status); }

DYLD_INTERPOSE(rm_open, open)
DYLD_INTERPOSE(rm_open_nc, open_nocancel_v)
DYLD_INTERPOSE(rm_openat, openat)
DYLD_INTERPOSE(rm_openat_nc, openat_nocancel_v)
DYLD_INTERPOSE(rm_stat, stat)
DYLD_INTERPOSE(rm_lstat, lstat)
DYLD_INTERPOSE(rm_fstat, fstat)
DYLD_INTERPOSE(rm_fstatat, fstatat)
DYLD_INTERPOSE(rm_access, access)
DYLD_INTERPOSE(rm_faccessat, faccessat)
DYLD_INTERPOSE(rm_opendir, opendir)
DYLD_INTERPOSE(rm_opendir2, __opendir2)
DYLD_INTERPOSE(rm_fdopendir, fdopendir)
DYLD_INTERPOSE(rm_readdir, readdir)
DYLD_INTERPOSE(rm_readdir_r, readdir_r)
DYLD_INTERPOSE(rm_readlink, readlink)
DYLD_INTERPOSE(rm_readlinkat, readlinkat)
DYLD_INTERPOSE(rm_realpath, realpath_plain)
DYLD_INTERPOSE(rm_realpath_ext, realpath_ext)
DYLD_INTERPOSE(rm_fcntl, fcntl)
DYLD_INTERPOSE(rm_chdir, chdir)
DYLD_INTERPOSE(rm_getcwd, getcwd)
DYLD_INTERPOSE(rm_execve, execve)
DYLD_INTERPOSE(rm_posix_spawn, posix_spawn)
DYLD_INTERPOSE(rm_waitpid, waitpid)
DYLD_INTERPOSE(rm_wait4, wait4)
DYLD_INTERPOSE(rm_wait3, wait3)
DYLD_INTERPOSE(rm_wait, wait)
DYLD_INTERPOSE(rm_waitid, waitid)
DYLD_INTERPOSE(rm_kill, kill)
DYLD_INTERPOSE(rm_killpg, killpg)
DYLD_INTERPOSE(rm__exit, _exit)

// ── lifecycle ────────────────────────────────────────────────────────────
// Test hook (frozen_selftest.py): 1 when this process loaded a manifest.
int rootmap_active(void) { return load_ok == 1; }

static void atfork_prepare(void) { pthread_mutex_lock(&rmu); }
static void atfork_parent(void) { pthread_mutex_unlock(&rmu); }
static void atfork_child(void) {
  pthread_mutex_init(&rmu, NULL);
  if (load_ok == 0) return;
  rfd = -1; receipt_path[0] = 0; exit_written = 0; events_logged = 0;
  c_mapped = c_frozen_direct = c_absent = c_unmapped = c_write_denied = c_dirent = 0;
  c_fd_patched = c_reverse = c_execs = c_spawns = c_reinjected = c_events_dropped = 0;
  c_reaped = c_signals = c_target = c_traversal = c_probe = 0;
  open_receipt();
  write_activation("fork");
}

static void at_exit(void) { write_exit("exit"); }

__attribute__((constructor)) static void init(void) {
  Dl_info info;
  if (dladdr((const void *)&init, &info) && info.dli_fname) snprintf(self_path, sizeof self_path, "%s", info.dli_fname);
  load_manifest();
  if (load_ok == 0) return;
  open_receipt();
  write_activation("load");
  pthread_atfork(atfork_prepare, atfork_parent, atfork_child);
  atexit(at_exit);
}

__attribute__((destructor)) static void fini(void) { write_exit("exit"); }
