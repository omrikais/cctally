// #901 probe: per-path write-byte attribution without root.
// Interposes write/pwrite/writev/pwritev (+ close) via DYLD_INSERT_LIBRARIES,
// accumulates bytes per fd, folds into a per-path table on close, and a
// background thread dumps the cumulative per-path table every WTRACE_PERIOD
// seconds (fractional allowed) to $WTRACE_OUT.<pid> (one JSON object per line:
// {"t":..., "pid":..., "dropped":..., "paths":{path:[bytes,calls]}}), plus a
// final snapshot to $WTRACE_OUT.<pid>.exit.
//
// Accounting is lossless under concurrency (#901 SR-003): a close folds its
// descriptor's bytes into the live table under `mu`, and a snapshot copies the
// live table and adds the still-open descriptors while holding the same `mu`,
// so a fold can never land in a snapshot copy and vanish. A path that does not
// fit the table is counted in "dropped", which invalidates the evidence; it is
// never silently discarded. `selftest.py` is the regression for both.
//
// WAL frame log (#901 SR-014, spec §6.3 "Deletion receipts"): for a path that
// ends in "-wal", a 24-byte write at offset 32 + k x (P + 24) is a frame header
// and is logged as {t, path, frame, pgno, commit, salt} (frame = k + 1; P from
// the 32-byte WAL header the same process wrote at offset 0, else
// $WTRACE_WAL_PAGE, default 4096), and a 32-byte write at offset 0 as a WAL
// header record (frame 0, pgno = page size, commit = checkpoint sequence). The
// Every snapshot also carries "footprintPeak", this process's
// ri_lifetime_max_phys_footprint (RUSAGE_INFO_V4), so the exit snapshot of every
// process the interposer is loaded into - dashboards, catch-ups, hooks and the
// workers they launch - records its terminal high-water mark (spec §6.3).
// The records sit in a bounded ring (WTRACE_FRAME_RING entries, default 1<<20)
// flushed with every snapshot to $WTRACE_OUT.<pid>.frames (and at exit). A
// record that finds the ring full is counted in the snapshot's "framesDropped"
// and its 24 bytes in "dropped", so the evidence is invalid, never lost.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <pthread.h>
#include <sys/uio.h>
#include <sys/param.h>
#include <sys/time.h>
#include <stdatomic.h>
#include <libproc.h>
#include <sys/resource.h>

#define DYLD_INTERPOSE(_r, _e) \
  __attribute__((used)) static struct { const void *r; const void *e; } _interpose_##_e \
  __attribute__((section("__DATA,__interpose"))) = {(const void *)(unsigned long)&_r, (const void *)(unsigned long)&_e};

#define MAXFD 65536
static _Atomic unsigned long long fd_bytes[MAXFD];
static _Atomic unsigned long long fd_calls[MAXFD];
static volatile char fd_kind[MAXFD];  // 0 unknown, 1 sqlite temp, 2 other, 3 WAL
static volatile signed char fd_wal[MAXFD];  // WAL path slot of a kind-3 descriptor

#define NWAL 32
struct frec { double t; unsigned int frame, pgno, commit, salt; int wal; };
static char wal_paths[NWAL][MAXPATHLEN];   // guarded by fmu
static unsigned int wal_page[NWAL];        // page size learnt from the header
static int nwal;
static struct frec *ring;                  // guarded by fmu
static size_t ring_cap, ring_len;
static unsigned long long frames_dropped;  // guarded by fmu
static pthread_mutex_t fmu = PTHREAD_MUTEX_INITIALIZER;
static unsigned int default_wal_page = 4096;
static __thread unsigned long long t_temp, t_other;
unsigned long long wtrace_thread_temp(void) { return t_temp; }
unsigned long long wtrace_thread_other(void) { return t_other; }

#define NPATH 8192
struct pent { char path[MAXPATHLEN]; unsigned long long bytes; unsigned long long calls; int used; };
static struct pent *ptab;    // live cumulative table; guarded by mu
static struct pent *snap;    // snapshot scratch; guarded by mu
static unsigned long long dropped;  // bytes that fit no table slot; guarded by mu
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static int ready;
static _Atomic int in_dump;  // test hook: 1 while a snapshot holds mu
static long pause_us;        // test hook: WTRACE_TEST_PAUSE_US, a pause inside the snapshot
int wtrace_in_dump(void) { return atomic_load(&in_dump); }

static unsigned long hstr(const char *s) { unsigned long h = 5381; int c; while ((c = *s++)) h = h * 33 + c; return h; }

// Caller holds mu.
static void padd(struct pent *tab, unsigned long long *drop, const char *p, unsigned long long b, unsigned long long c) {
  unsigned long h = hstr(p) % NPATH;
  for (int i = 0; i < NPATH; i++) {
    struct pent *e = &tab[(h + i) % NPATH];
    if (!e->used) { e->used = 1; strncpy(e->path, p, MAXPATHLEN - 1); e->bytes = b; e->calls = c; return; }
    if (strcmp(e->path, p) == 0) { e->bytes += b; e->calls += c; return; }
  }
  *drop += b;
}

static unsigned int be32(const unsigned char *b) { return ((unsigned)b[0] << 24) | ((unsigned)b[1] << 16) | ((unsigned)b[2] << 8) | b[3]; }

static double now_s(void) { struct timeval tv; gettimeofday(&tv, NULL); return tv.tv_sec + tv.tv_usec / 1e6; }

static void classify(int fd) {
  char p[MAXPATHLEN];
  if (fcntl(fd, F_GETPATH, p) != 0) { fd_kind[fd] = 2; return; }
  if (strstr(p, "etilqs_")) { fd_kind[fd] = 1; return; }
  size_t len = strlen(p);
  if (len > 4 && strcmp(p + len - 4, "-wal") == 0) {
    pthread_mutex_lock(&fmu);
    int slot = -1;
    for (int i = 0; i < nwal; i++) if (strcmp(wal_paths[i], p) == 0) { slot = i; break; }
    if (slot < 0 && nwal < NWAL) { slot = nwal++; strncpy(wal_paths[slot], p, MAXPATHLEN - 1); wal_page[slot] = 0; }
    pthread_mutex_unlock(&fmu);
    if (slot >= 0) { fd_wal[fd] = (signed char)slot; fd_kind[fd] = 3; return; }
  }
  fd_kind[fd] = 2;
}

// Log one WAL frame header (24 bytes) or WAL header (32 bytes at offset 0).
static void walrec(int fd, const void *buf, size_t n, off_t off) {
  const unsigned char *b = (const unsigned char *)buf;
  int slot = fd_wal[fd];
  pthread_mutex_lock(&fmu);
  unsigned int page = wal_page[slot] ? wal_page[slot] : default_wal_page;
  struct frec r;
  int keep = 0;
  if (n == 32 && off == 0) {
    wal_page[slot] = be32(b + 8);
    r = (struct frec){now_s(), 0, be32(b + 8), be32(b + 12), be32(b + 16), slot};
    keep = 1;
  } else if (n == 24 && off >= 32 && (off - 32) % (page + 24) == 0) {
    r = (struct frec){now_s(), (unsigned)((off - 32) / (page + 24)) + 1, be32(b), be32(b + 4), be32(b + 8), slot};
    keep = 1;
  }
  if (keep) {
    if (ring && ring_len < ring_cap) ring[ring_len++] = r;
    else { frames_dropped++; pthread_mutex_unlock(&fmu); pthread_mutex_lock(&mu); dropped += 24; pthread_mutex_unlock(&mu); return; }
  }
  pthread_mutex_unlock(&fmu);
}

static inline void wacct_at(int fd, const void *buf, ssize_t n, off_t off) {
  if (n > 0 && fd >= 0 && fd < MAXFD) {
    atomic_fetch_add(&fd_bytes[fd], (unsigned long long)n); atomic_fetch_add(&fd_calls[fd], 1ULL);
    if (!fd_kind[fd]) classify(fd);
    if (fd_kind[fd] == 1) t_temp += (unsigned long long)n; else t_other += (unsigned long long)n;
    if (fd_kind[fd] == 3 && buf && off >= 0 && (n == 24 || n == 32)) walrec(fd, buf, (size_t)n, off);
  }
}

static inline void wacct(int fd, ssize_t n) { wacct_at(fd, NULL, n, -1); }

static void fold(int fd) {
  if (!ready || fd < 0 || fd >= MAXFD) return;
  char p[MAXPATHLEN];
  if (fcntl(fd, F_GETPATH, p) != 0) snprintf(p, sizeof p, "<fd%d>", fd);
  pthread_mutex_lock(&mu);
  unsigned long long b = atomic_exchange(&fd_bytes[fd], 0ULL);
  unsigned long long c = atomic_exchange(&fd_calls[fd], 0ULL);
  if (b) padd(ptab, &dropped, p, b, c);
  pthread_mutex_unlock(&mu);
}

ssize_t my_write(int fd, const void *buf, size_t n) {
  ssize_t r = write(fd, buf, n);
  off_t end = (r == 24 || r == 32) ? lseek(fd, 0, SEEK_CUR) : -1;
  wacct_at(fd, buf, r, end >= 0 ? end - r : -1); return r;
}
ssize_t my_pwrite(int fd, const void *buf, size_t n, off_t o) { ssize_t r = pwrite(fd, buf, n, o); wacct_at(fd, buf, r, o); return r; }
ssize_t my_writev(int fd, const struct iovec *v, int c) { ssize_t r = writev(fd, v, c); wacct(fd, r); return r; }
ssize_t my_pwritev(int fd, const struct iovec *v, int c, off_t o) { ssize_t r = pwritev(fd, v, c, o); wacct(fd, r); return r; }
int my_close(int fd) { fold(fd); if (fd >= 0 && fd < MAXFD) { fd_kind[fd] = 0; fd_wal[fd] = 0; } return close(fd); }
DYLD_INTERPOSE(my_write, write)
DYLD_INTERPOSE(my_pwrite, pwrite)
DYLD_INTERPOSE(my_writev, writev)
DYLD_INTERPOSE(my_pwritev, pwritev)
DYLD_INTERPOSE(my_close, close)

static void dump(FILE *f) {
  // Snapshot = live table + still-open descriptors, built entirely under mu.
  pthread_mutex_lock(&mu);
  atomic_store(&in_dump, 1);
  memcpy(snap, ptab, sizeof(struct pent) * NPATH);
  unsigned long long drop = dropped;
  for (int fd = 0; fd < MAXFD; fd++) {
    unsigned long long b = atomic_load(&fd_bytes[fd]);
    if (!b) continue;
    char p[MAXPATHLEN];
    if (fcntl(fd, F_GETPATH, p) != 0) snprintf(p, sizeof p, "<fd%d>", fd);
    padd(snap, &drop, p, b, atomic_load(&fd_calls[fd]));
  }
  if (pause_us > 0) usleep((useconds_t)pause_us);
  struct timeval tv; gettimeofday(&tv, NULL);
  pthread_mutex_lock(&fmu);
  unsigned long long fdrop = frames_dropped;
  pthread_mutex_unlock(&fmu);
  struct rusage_info_v4 ri;
  long long peak = proc_pid_rusage(getpid(), RUSAGE_INFO_V4, (rusage_info_t *)&ri) == 0
                   ? (long long)ri.ri_lifetime_max_phys_footprint : -1;
  fprintf(f, "{\"t\":%ld.%06d,\"pid\":%d,\"dropped\":%llu,\"framesDropped\":%llu,\"footprintPeak\":%lld,\"paths\":{", (long)tv.tv_sec, (int)tv.tv_usec, getpid(), drop, fdrop, peak);
  int first = 1;
  for (int i = 0; i < NPATH; i++) if (snap[i].used) {
    fprintf(f, "%s\"%s\":[%llu,%llu]", first ? "" : ",", snap[i].path, snap[i].bytes, snap[i].calls); first = 0;
  }
  fprintf(f, "}}\n");
  atomic_store(&in_dump, 0);
  pthread_mutex_unlock(&mu);
  fflush(f);
}

// Append the ring's records to `f` (JSON lines) and empty it.
static void flush_frames(FILE *f) {
  pthread_mutex_lock(&fmu);
  for (size_t i = 0; i < ring_len; i++) {
    struct frec *r = &ring[i];
    fprintf(f, "{\"t\":%.6f,\"path\":\"%s\",\"frame\":%u,\"pgno\":%u,\"commit\":%u,\"salt\":%u}\n",
            r->t, wal_paths[r->wal], r->frame, r->pgno, r->commit, r->salt);
  }
  ring_len = 0;
  pthread_mutex_unlock(&fmu);
  fflush(f);
}

// Test hook: one snapshot appended to `path`. Returns 0 on success.
int wtrace_dump_to(const char *path) {
  FILE *f = fopen(path, "a"); if (!f) return -1; dump(f); fclose(f); return 0;
}

// Test hook: flush the frame ring to `path`. Returns 0 on success.
int wtrace_frames_to(const char *path) {
  FILE *f = fopen(path, "a"); if (!f) return -1; flush_frames(f); fclose(f); return 0;
}

// Test hook: resize (and empty) the frame ring.
void wtrace_set_frame_ring(size_t cap) {
  pthread_mutex_lock(&fmu);
  free(ring); ring = calloc(cap ? cap : 1, sizeof(struct frec)); ring_cap = ring ? cap : 0; ring_len = 0; frames_dropped = 0;
  pthread_mutex_unlock(&fmu);
}

static void *dumper(void *arg) {
  const char *out = getenv("WTRACE_OUT");
  double period = getenv("WTRACE_PERIOD") ? atof(getenv("WTRACE_PERIOD")) : 5.0;
  if (period <= 0) period = 5.0;
  char path[MAXPATHLEN]; snprintf(path, sizeof path, "%s.%d", out, getpid());
  char fpath[MAXPATHLEN]; snprintf(fpath, sizeof fpath, "%s.%d.frames", out, getpid());
  FILE *f = fopen(path, "a");
  FILE *ff = fopen(fpath, "a");
  if (!f || !ff) return NULL;  // no trace file: the analyzer refuses the run
  for (;;) { usleep((useconds_t)(period * 1e6)); flush_frames(ff); dump(f); }
  return NULL;
}

__attribute__((destructor)) static void fini(void) {
  const char *out = getenv("WTRACE_OUT"); if (!out) return;
  char fpath[MAXPATHLEN]; snprintf(fpath, sizeof fpath, "%s.%d.frames", out, getpid());
  FILE *ff = fopen(fpath, "a"); if (ff) { flush_frames(ff); fclose(ff); }
  char path[MAXPATHLEN]; snprintf(path, sizeof path, "%s.%d.exit", out, getpid());
  FILE *f = fopen(path, "a"); if (!f) return; dump(f); fclose(f);
}

// Fork safety: a child forked while another thread holds `mu` or `fmu` would
// inherit the held lock and deadlock in the interposed close() that Python's
// subprocess (close_fds) calls between fork and exec. Take both locks in their
// nesting order (mu, then fmu, as dump() does) before fork, and release them in
// the parent and in the child, whose forking thread owns the copies.
static void atfork_prepare(void) { pthread_mutex_lock(&mu); pthread_mutex_lock(&fmu); }
static void atfork_release(void) { pthread_mutex_unlock(&fmu); pthread_mutex_unlock(&mu); }

__attribute__((constructor)) static void init(void) {
  pthread_atfork(atfork_prepare, atfork_release, atfork_release);
  ptab = calloc(NPATH, sizeof(struct pent));
  snap = calloc(NPATH, sizeof(struct pent));
  pause_us = getenv("WTRACE_TEST_PAUSE_US") ? atol(getenv("WTRACE_TEST_PAUSE_US")) : 0;
  if (getenv("WTRACE_WAL_PAGE")) default_wal_page = (unsigned)atol(getenv("WTRACE_WAL_PAGE"));
  ring_cap = getenv("WTRACE_FRAME_RING") ? (size_t)atol(getenv("WTRACE_FRAME_RING")) : ((size_t)1 << 20);
  ring = calloc(ring_cap ? ring_cap : 1, sizeof(struct frec));
  if (!ring) ring_cap = 0;
  ready = 1;
  if (getenv("WTRACE_OUT")) { pthread_t t; pthread_create(&t, NULL, dumper, NULL); pthread_detach(t); }
}
