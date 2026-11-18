#!/usr/bin/env python3
"""Write-budget preparation v2 (#937): the relocated full-size fixture.

The §6.4 write-budget leg measures a full-size production-shaped fixture
whose stored paths name a canonical root. Preparation v2 keeps that fixture in
an APFS sparse-bundle disk image on a working root ``W`` (on the backing
volume) and attaches it, without sudo, at the canonical root ``C`` the
fixture's marker names, so every stored path names a real file at its stored
spelling. This module owns the parts of that preparation that are not
dashboard flows:

- ``ImageLifecycle``: the only code that creates, attaches and detaches the
  image. Preconditions refuse with a remedy and never act on their own; the
  teardown reaps only recorded processes, reports (never signals) unknown
  holders, detaches only the owned device and removes only an owned, empty
  mountpoint.
- ``clonefile_bundle``: one ``clonefile(2)`` of a detached bundle directory,
  with no copy fallback.
- ``source_inventory``, ``volume_device``, ``inventory_record`` and
  ``bundle_seal``: the identities P0, P1 and P2 bind.
- ``BudgetWatchdog`` and ``disk_write_meter``: the per-job write and wall-clock
  budget on the backing physical disk's cumulative ``Bytes (Write)`` counter,
  with an aggregate ledger across jobs.
- ``acquire_host_lock``: the runner host lock, retried for a bounded time.
- ``StoreEquivalence``: P0's comparison of the retained stores with the
  producer's normalized ones (byte-derived families, carrier invariants and
  the cursor population).
- ``identity_equality`` and ``write_admission_inputs``: P2's sealed identity
  check and the inputs of catchup's ``image`` admission mode.
- ``content_digest``: the two-sided full-content digest, also runnable on its
  own as ``python3 -I bench/soak_image.py digest <dir>``.

Standard library only; nothing outside it is imported at module level, so
the digest entry point runs from any scratch directory.
"""
from __future__ import annotations

import ctypes
import dataclasses
import errno
import hashlib
import importlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import plistlib
import re
import signal
import sqlite3
import stat as stat_mod
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent

#: The fixture marker's root: the canonical root every stored path names.
CANONICAL_ROOT_DEFAULT = pathlib.Path(
    "/private/tmp/cctally-dashboard-evidence-f7709be6a636")
#: Volume-root entries that belong to the volume or to P0, never to the
#: fixture: excluded from every inventory and digest.
HOUSEKEEPING = frozenset({
    ".metadata_never_index", ".fseventsd", ".Spotlight-V100", ".Trashes",
    ".TemporaryItems", ".p0-retained"})
IMAGE_SIZE_GIB = 300
IMAGE_VOLUME_NAME = "cctally-soak"
DETACH_TIMEOUT_S = 60
#: The backing volume's cleanup reserve (free-space guard).
FREE_RESERVE_BYTES = 20 * 1024 ** 3
#: The bundle files a seal covers besides the bands.
BUNDLE_META_FILES = ("Info.plist", "Info.bckup", "token")
#: The device-sensitive journal sidecars (F18) and the store files P2 binds.
JOURNAL_CARRIERS = (".quota-observation-keys", ".quota-observation-keys.meta",
                    ".segment-summaries")
STORE_FILES = tuple(f"{db}{suffix}" for db in ("cache.db", "conversations.db",
                                               "stats.db")
                    for suffix in ("", "-wal", "-shm"))
#: Cursor families per provider (spec §5.3 step 6).
CURSOR_FAMILIES = {
    "claude": (("cache.db", "session_files"),
               ("conversations.db", "conversation_source_files")),
    "codex": (("cache.db", "codex_session_files"),
              ("conversations.db", "codex_conversation_source_files")),
}
#: The families whose rows carry a file identity (inode, size, mtime).
IDENTITY_FAMILIES = ("conversation_source_files", "codex_session_files",
                     "codex_conversation_source_files")
_FIRST = 20
#: The per-job owner token every process a v2 step starts inherits (#937
#: SD7). A detached product worker carries it because the product's spawners
#: inherit their environment, so a volume holder whose environment holds
#: exactly this job's token is that job's own detached worker.
OWNER_TOKEN_ENV = "CCTALLY_BENCH_OWNER_TOKEN"
#: Teardown step 2's bound: enumerate, reap the owned workers, re-enumerate.
OWNED_REAP_ROUNDS = 3
#: The product's own worker records under APP_DIR, where a tree has them:
#: the stats workers' launch ledgers and the Codex worker attempt records.
#: Corroboration only; ownership is proven by the token.
PRODUCT_LAUNCH_LEDGERS = ("stats-epoch-rebuild.launches.json",
                          "stats-heal.launches.json")
PRODUCT_CODEX_RECORD_DIR = "codex-workers"


class ImageRefusal(RuntimeError):
    """A precondition or a check that refuses the step; ``remedy`` says what
    the operator does. Nothing is undone or removed automatically."""

    def __init__(self, problem: str, remedy: str):
        super().__init__(f"{problem}; remedy: {remedy}")
        self.problem = problem
        self.remedy = remedy


class ImageInterrupted(RuntimeError):
    """SIGTERM, SIGINT or SIGHUP arrived while the image was owned; the caller
    unwinds to its ``close()``."""

    def __init__(self, signum: int):
        super().__init__(f"interrupted by signal {signum}")
        self.signum = signum


class HolderEnumerationFailed(RuntimeError):
    """``lsof`` could not say who holds files on the volume."""


@dataclasses.dataclass
class AttachRecord:
    device_node: str
    st_dev: int
    mount_options: list
    owner_uid: "int | None"
    case_sensitive: bool
    mountpoint: str
    bundle: str
    image_path: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class TeardownRecord:
    problems: list = dataclasses.field(default_factory=list)
    holders: list = dataclasses.field(default_factory=list)
    detached: bool = False
    canonical_absent: bool = False
    reaped: list = dataclasses.field(default_factory=list)
    signals: list = dataclasses.field(default_factory=list)
    appender_error: "str | None" = None
    owned_workers: list = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict:
        out = dataclasses.asdict(self)
        out["ownedWorkers"] = out.pop("owned_workers")
        return out


# ── small helpers ──────────────────────────────────────────────────────────

def _under(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def _decode(data) -> str:
    if data is None:
        return ""
    return data if isinstance(data, str) else data.decode("utf-8", "replace")


def _tail(data, limit: int = 300) -> str:
    return " ".join(_decode(data).split())[-limit:]


_MOUNT_LINE = re.compile(r"^(\S+) on (.+) \(([^()]*)\)$")


def parse_mount_table(text: str) -> "list[dict]":
    """``mount`` output as [{device, mountpoint, options}]."""
    out = []
    for line in _decode(text).splitlines():
        match = _MOUNT_LINE.match(line.strip())
        if match:
            out.append({"device": match.group(1),
                        "mountpoint": match.group(2),
                        "options": [o.strip() for o in match.group(3).split(",")
                                    if o.strip()]})
    return out


def lsof_holders(mountpoint, run) -> "list[dict]":
    """Every process holding a file on the volume mounted at `mountpoint`, as
    [{pid, command, files: [{fd, name}]}]. lsof exits 1 with no output when
    nothing is open; any other failure raises, because an enumeration that
    failed cannot prove the volume is free."""
    try:
        proc = run(["lsof", "-n", "-P", "-F", "pcfn", str(mountpoint)])
    except (OSError, subprocess.SubprocessError) as exc:
        raise HolderEnumerationFailed(f"lsof did not run: {exc}") from exc
    out, err = _decode(proc.stdout), _decode(proc.stderr)
    if proc.returncode == 1 and not out.strip() and not err.strip():
        return []
    if proc.returncode != 0:
        raise HolderEnumerationFailed(
            f"lsof exited {proc.returncode}: {_tail(err) or _tail(out)}")
    holders, current, handle = [], None, None
    for line in out.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            current = {"pid": int(value), "command": None, "files": []}
            holders.append(current)
            handle = None
        elif current is None:
            raise HolderEnumerationFailed(f"unparseable lsof output: {line!r}")
        elif tag == "c":
            current["command"] = value
        elif tag == "f":
            handle = {"fd": value, "name": None}
            current["files"].append(handle)
        elif tag == "n":
            if handle is None:
                handle = {"fd": None, "name": None}
                current["files"].append(handle)
            handle["name"] = value
    return holders


def process_start_identity(pid: int) -> "str | None":
    """A value that changes when `pid` is reused; None when it is gone."""
    try:
        proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(proc.stdout.split())
    return f"lstart:{text}" if proc.returncode == 0 and text else None


def _process_uid(pid: int) -> "int | None":
    try:
        proc = subprocess.run(["ps", "-o", "uid=", "-p", str(int(pid))],
                              capture_output=True, text=True, timeout=10)
        return int(proc.stdout.strip()) if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _procargs2_environment(pid: int) -> "dict | None":
    """macOS: the environment `pid` started with, from KERN_PROCARGS2
    (argc, the exec path, argv, then the environment, NUL-separated). Exact,
    unlike parsing `ps -E`, whose command words and environment share one
    line."""
    libc = ctypes.CDLL(None, use_errno=True)
    size = ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    argmax = ctypes.c_int(0)
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax),
                   ctypes.byref(size), None, 0) != 0 or argmax.value <= 0:
        return None
    buffer = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(argmax.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, int(pid)), 3, buffer,
                   ctypes.byref(size), None, 0) != 0:
        return None
    raw = buffer.raw[:size.value]
    if len(raw) < 4:
        return None
    argc = int.from_bytes(raw[:4], sys.byteorder)
    rest = raw[4:]
    end = rest.find(b"\0")
    if end < 0:
        return None
    while end < len(rest) and rest[end] == 0:
        end += 1
    parts = rest[end:].split(b"\0")
    environment = {}
    for item in parts[argc:]:
        if not item:
            break
        key, sep, value = item.partition(b"=")
        if sep:
            environment[os.fsdecode(key)] = os.fsdecode(value)
    return environment


def process_environment(pid: int) -> "dict | None":
    """The environment of `pid`, a process of this uid only; None when it
    cannot be read (gone, another uid, or no reader on this platform). An
    unreadable environment never proves ownership."""
    pid = int(pid)
    if sys.platform.startswith("linux"):
        path = pathlib.Path(f"/proc/{pid}/environ")
        try:
            if path.stat().st_uid != os.getuid():
                return None
            raw = path.read_bytes()
        except OSError:
            return None
        out = {}
        for item in raw.split(b"\0"):
            key, sep, value = item.partition(b"=")
            if sep:
                out[os.fsdecode(key)] = os.fsdecode(value)
        return out
    if sys.platform != "darwin" or _process_uid(pid) != os.getuid():
        return None
    try:
        return _procargs2_environment(pid)
    except (OSError, ValueError, AttributeError):
        return None


def product_worker_records(app_dir) -> "dict[int, list]":
    """{pid: [(record name, recorded birth)]} from the product's own worker
    records under `app_dir`, where the tree has them (the stats workers'
    launch ledgers; the Codex worker attempt records). A tree without them
    (the 56e66f07a baseline) yields {}; an unreadable record is skipped,
    because these records corroborate ownership and never prove it."""
    app = pathlib.Path(app_dir)
    out: "dict[int, list]" = {}

    def note(pid, name, birth=None):
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            out.setdefault(pid, []).append((name, birth))

    for name in PRODUCT_LAUNCH_LEDGERS:
        try:
            ledger = json.loads((app / name).read_text())
        except (OSError, ValueError):
            continue
        for entry in (ledger.get("entries") if isinstance(ledger, dict)
                      else None) or []:
            if isinstance(entry, dict):
                note(entry.get("pid"), name, entry.get("birth"))
    try:
        records = sorted((app / PRODUCT_CODEX_RECORD_DIR).glob("*.json"))
    except OSError:
        records = []
    for path in records:
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            note(record.get("pid"), f"{PRODUCT_CODEX_RECORD_DIR}/{path.name}")
    return out


# ── the image lifecycle owner ──────────────────────────────────────────────

class ImageLifecycle:
    """The one owner of the image at the canonical root (spec §5.2).

    `run(argv, **kwargs)` runs every tool (hdiutil, mount, lsof); `lsof`, when
    given, replaces holder enumeration (`lsof(mountpoint) -> [holder]`);
    `clock`, `kill` and `sleep` drive reaping. Every child gets `env`, which
    defaults to the inherited environment with `TMPDIR=W/tmp/image/` and
    `PYTHONDONTWRITEBYTECODE=1`.

    With `owner_token`, a holder this owner did not start is an **owned
    detached worker** when its environment (`read_environment(pid)`) carries
    exactly that token under `OWNER_TOKEN_ENV`; its start identity is taken
    at classification and re-checked before every signal. Teardown reaps
    owned workers after the recorded families; a holder whose ownership is
    not proven (no token, another token, an unreadable environment) stays an
    unknown holder: reported, never signalled. `worker_records()` returns
    the product's own records (`product_worker_records`), which only
    corroborate."""

    SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

    def __init__(self, *, working_root: pathlib.Path,
                 canonical_root: pathlib.Path, run=subprocess.run, lsof=None,
                 clock=time.monotonic, env: "dict | None" = None,
                 kill=os.kill, sleep=time.sleep, start_identity=None,
                 grace_s: float = 10.0, kill_wait_s: float = 5.0,
                 owner_token: "str | None" = None, read_environment=None,
                 worker_records=None):
        self.working_root = pathlib.Path(working_root)
        self.canonical_root = pathlib.Path(canonical_root)
        self.run = run
        self.lsof = lsof
        self.clock = clock
        self.kill = kill
        self.sleep = sleep
        self.start_identity = start_identity or process_start_identity
        self.grace_s = grace_s
        self.kill_wait_s = kill_wait_s
        self._env = env
        self._attached: "AttachRecord | None" = None
        self._attaching_device: "str | None" = None
        self._created_mountpoint = False
        self._processes: "dict[int, str | None]" = {}
        self._appender = None
        self._closing = False
        self._closed: "TeardownRecord | None" = None
        self._previous_handlers: dict = {}
        self.interrupted = False
        self.signals_received: "list[int]" = []
        self.owner_token = owner_token
        self.read_environment = read_environment or process_environment
        self.worker_records = worker_records or (
            lambda: product_worker_records(self.canonical_root / "data"))
        #: {(pid, start identity): owned-worker record}, across teardowns.
        self._owned: dict = {}
        #: Problems of an aborted attach, reported by `close()`.
        self._abort_problems: "list[str]" = []

    # -- children ---------------------------------------------------------
    @property
    def env(self) -> dict:
        if self._env is None:
            tmp = self.working_root / "tmp" / "image"
            tmp.mkdir(parents=True, exist_ok=True)
            self._env = {**os.environ, "TMPDIR": str(tmp) + os.sep,
                         "PYTHONDONTWRITEBYTECODE": "1"}
        return self._env

    def _run(self, argv, timeout: float):
        return self.run(list(argv), capture_output=True, timeout=timeout,
                        env=self.env)

    def _mounts(self) -> "list[dict]":
        proc = self._run(["mount"], 60)
        if proc.returncode != 0:
            raise ImageRefusal(f"mount exited {proc.returncode}",
                               "inspect the host's mount table by hand")
        return parse_mount_table(proc.stdout)

    def _require_under_working_root(self, path: pathlib.Path, what: str) -> None:
        if not _under(os.path.realpath(path), os.path.realpath(self.working_root)):
            raise ImageRefusal(
                f"{what} {path} is not under the working root {self.working_root}",
                "keep every bundle under the working root on the backing volume")

    # -- create / attach --------------------------------------------------
    def create(self, bundle: pathlib.Path, *, size_gib: int = IMAGE_SIZE_GIB,
               required_free_bytes: int = 0) -> None:
        """A new case-insensitive APFS sparse bundle at `bundle`, a new path
        under the working root."""
        bundle = pathlib.Path(bundle)
        self._require_under_working_root(bundle.parent, "bundle directory")
        if os.path.lexists(bundle):
            raise ImageRefusal(f"bundle path {bundle} already exists",
                               "choose a new unique bundle path; never reuse one")
        if required_free_bytes:
            st = os.statvfs(self.working_root)
            free = st.f_bavail * st.f_frsize
            if free < required_free_bytes:
                raise ImageRefusal(
                    f"the backing volume has {free} free bytes, below the "
                    f"step's required {required_free_bytes}",
                    "free space on the backing volume")
        proc = self._run(["hdiutil", "create", "-type", "SPARSEBUNDLE", "-fs",
                          "APFS", "-size", f"{int(size_gib)}g", "-volname",
                          IMAGE_VOLUME_NAME, "-plist", str(bundle)], 600)
        if proc.returncode != 0:
            raise ImageRefusal(
                f"hdiutil create exited {proc.returncode}: {_tail(proc.stderr)}",
                f"inspect {bundle}; a partial bundle is left for the operator")
        try:
            named = plistlib.loads(proc.stdout if isinstance(proc.stdout, bytes)
                                   else proc.stdout.encode())
        except Exception as exc:  # noqa: BLE001 - any parse failure refuses
            raise ImageRefusal(f"hdiutil create printed no plist: {exc}",
                               f"inspect {bundle}") from exc
        if str(bundle) not in (named if isinstance(named, list) else []):
            raise ImageRefusal(f"hdiutil create did not name {bundle}: {named}",
                               f"inspect {bundle}")

    def attach(self, bundle: pathlib.Path) -> AttachRecord:
        """Attach `bundle` at the canonical root and verify what is mounted
        there. Refuses, never repairs, a stale mount or a leftover root."""
        bundle = pathlib.Path(bundle)
        root = str(self.canonical_root)
        if self._attached is not None:
            raise ImageRefusal(f"an image is already attached at {root}",
                               "detach it through this owner first")
        self._require_under_working_root(bundle, "bundle")
        if not bundle.is_dir():
            raise ImageRefusal(f"bundle {bundle} is not a directory",
                               "create or clone the bundle first")
        if os.path.lexists(root):
            raise ImageRefusal(
                f"the canonical root {root} already exists",
                f"if it is a stale mount, run: hdiutil detach {root}; if it "
                f"is a leftover empty directory, run: rmdir {root}; it is "
                "never removed automatically")
        stale = [m for m in self._mounts() if _under(m["mountpoint"], root)]
        if stale:
            raise ImageRefusal(
                f"a mount is at or under {root}: "
                + ", ".join(f"{m['device']} on {m['mountpoint']}" for m in stale),
                "run: " + "; ".join(f"hdiutil detach {m['device']}"
                                     for m in stale))
        os.mkdir(root, 0o755)
        self._created_mountpoint = True
        try:
            return self._attach_owned(bundle, root)
        except BaseException:
            self._abort_attach()
            raise

    def _attach_owned(self, bundle: pathlib.Path, root: str) -> AttachRecord:
        proc = self._run(["hdiutil", "attach", "-nobrowse", "-noautoopen",
                          "-owners", "on", "-mountpoint", root, str(bundle),
                          "-plist"], 300)
        if proc.returncode != 0:
            raise ImageRefusal(
                f"hdiutil attach exited {proc.returncode}: {_tail(proc.stderr)}",
                f"inspect {bundle} and the mount table")
        plist = plistlib.loads(proc.stdout if isinstance(proc.stdout, bytes)
                               else proc.stdout.encode())
        mounted = [e for e in plist.get("system-entities") or []
                   if e.get("mount-point") == root]
        if len(mounted) != 1 or not mounted[0].get("dev-entry"):
            raise ImageRefusal(f"hdiutil attach did not mount exactly one "
                               f"volume at {root}", f"inspect {bundle}")
        device = mounted[0]["dev-entry"]
        self._attaching_device = device
        if os.path.realpath(root) != root:
            raise ImageRefusal(f"realpath({root}) is {os.path.realpath(root)}",
                               "the canonical root must not pass through a link")
        st = os.stat(root)
        at = [m for m in self._mounts() if m["mountpoint"] == root]
        if len(at) != 1 or at[0]["device"] != device:
            raise ImageRefusal(
                f"the mount table does not name exactly {device} at {root}: {at}",
                "inspect the mount table")
        info = self._run(["hdiutil", "info", "-plist"], 60)
        if info.returncode != 0:
            raise ImageRefusal(f"hdiutil info exited {info.returncode}",
                               "inspect hdiutil info")
        images = plistlib.loads(info.stdout if isinstance(info.stdout, bytes)
                                else info.stdout.encode()).get("images") or []
        owning = [im for im in images
                  if any(e.get("dev-entry") == device
                         for e in im.get("system-entities") or [])]
        if len(owning) != 1:
            raise ImageRefusal(f"hdiutil info names {len(owning)} images for "
                               f"{device}", "inspect hdiutil info")
        image_path = owning[0].get("image-path") or ""
        if not _under(os.path.realpath(image_path),
                      os.path.realpath(self.working_root)):
            raise ImageRefusal(
                f"the image behind {device} is {image_path}, not a bundle "
                f"under the working root {self.working_root}",
                "attach only bundles under the working root")
        never_index = os.path.join(root, ".metadata_never_index")
        with open(never_index, "a"):
            pass
        os.makedirs(os.path.join(root, ".fseventsd"), exist_ok=True)
        with open(os.path.join(root, ".fseventsd", "no_log"), "a"):
            pass
        case_sensitive = not os.path.exists(
            os.path.join(root, ".METADATA_NEVER_INDEX"))
        record = AttachRecord(
            device_node=device, st_dev=st.st_dev,
            mount_options=list(at[0]["options"]),
            owner_uid=owning[0].get("owner-uid"),
            case_sensitive=case_sensitive, mountpoint=root,
            bundle=str(bundle), image_path=image_path)
        self._attached = record
        self._attaching_device = None
        return record

    def _abort_attach(self) -> None:
        """A failed attach: detach the device this owner just attached (it is
        ours) and remove the mountpoint it created when that is empty."""
        device, self._attaching_device = self._attaching_device, None
        if device is not None:
            try:
                proc = self._run(["hdiutil", "detach", device], DETACH_TIMEOUT_S)
                failure = (None if proc.returncode == 0 else
                           f"exited {proc.returncode}: "
                           f"{_tail(proc.stderr) or _tail(proc.stdout)}")
            except (OSError, subprocess.SubprocessError) as exc:
                failure = f"{type(exc).__name__}: {exc}"
            if failure is not None:
                self._abort_problems.append(
                    f"the failed attach's hdiutil detach {device} {failure}; "
                    f"remedy: run hdiutil detach {device}")
        self._remove_owned_mountpoint()

    def _remove_owned_mountpoint(self) -> None:
        root = str(self.canonical_root)
        if (self._created_mountpoint and os.path.isdir(root)
                and not os.path.islink(root) and not os.path.ismount(root)
                and not os.listdir(root)):
            os.rmdir(root)
            self._created_mountpoint = False

    # -- ownership ----------------------------------------------------------
    def register_process(self, pid: int) -> None:
        """Record a process a step started (pid plus its start identity)."""
        self._processes[int(pid)] = self.start_identity(int(pid))

    def set_appender(self, stop) -> None:
        """`stop()` stops the workload appender; its exception invalidates
        the run (teardown records it)."""
        self._appender = stop

    def recorded_identity(self, pid: int) -> "str | None":
        """The start identity recorded for a process a step started."""
        return self._processes.get(int(pid))

    def holders(self) -> "list[dict]":
        """Every process holding a file on the attached volume, each marked
        `recorded` when this owner started it, and every other one marked
        `owned` (with its `proof`) when it is this job's detached worker."""
        if self._attached is None:
            return []
        if self.lsof is not None:
            found = self.lsof(self.canonical_root)
        else:
            found = lsof_holders(self.canonical_root,
                                 lambda argv: self._run(argv, 60))
        recorded = set(self._processes) | {os.getpid()}
        out = []
        for holder in found:
            pid = int(holder["pid"])
            entry = {**holder, "recorded": pid in recorded}
            if not entry["recorded"]:
                entry["owned"], entry["proof"] = self._ownership(pid)
            out.append(entry)
        return out

    def _ownership(self, pid: int) -> "tuple[bool, dict]":
        """(owned, proof): owned only when the environment carries exactly
        this job's token; anything else is not proven."""
        if self.owner_token is None:
            return False, {"envToken": False, "reason": "no owner token"}
        try:
            environment = self.read_environment(pid)
        except Exception:  # noqa: BLE001 - unreadable is not proven
            environment = None
        if environment is None:
            return False, {"envToken": None,
                           "reason": "environment unreadable"}
        if environment.get(OWNER_TOKEN_ENV) != self.owner_token:
            return False, {"envToken": False,
                           "reason": "no token, or another job's"}
        return True, {"envToken": True}

    def _product_record(self, pid: int, identity: "str | None") -> "str | None":
        try:
            records = self.worker_records() or {}
        except Exception:  # noqa: BLE001 - corroboration only
            return None
        lstart = (identity or "").partition(":")[2]
        for name, birth in records.get(int(pid)) or ():
            if birth is None or " ".join(str(birth).split()) == lstart:
                return name
        return None

    def reap_owned_workers(self) -> "tuple[list, list]":
        """Teardown step 2 for detached workers (spec §5.2, #937 SD7):
        enumerate the volume's holders, reap every owned detached worker
        (its start identity re-checked before each signal), re-enumerate,
        within `OWNED_REAP_ROUNDS`. Returns (owned-worker records, problems).
        An enumeration failure stops the rounds; the caller's own holder
        check then reports it."""
        if self._attached is None or self.owner_token is None:
            return [], []

        def owned_now():
            """[(holder, pid, identity)] of the owned holders alive now, or
            None when the enumeration failed."""
            try:
                found = [h for h in self.holders() if h.get("owned")]
            except HolderEnumerationFailed:
                return None
            alive = []
            for holder in found:
                pid = int(holder["pid"])
                identity = self.start_identity(pid)
                if identity is not None:  # else gone since the enumeration
                    alive.append((holder, pid, identity))
            return alive

        def record(holder, pid, identity, outcome):
            self._owned[(pid, identity)] = {
                "pid": pid, "command": holder.get("command"),
                "startIdentity": identity,
                "proof": {"envToken": True,
                          "productRecord": self._product_record(pid,
                                                                identity)},
                "outcome": outcome}

        for _round in range(OWNED_REAP_ROUNDS):
            alive = owned_now()
            if not alive:
                break
            for holder, pid, identity in alive:
                prior = self._owned.get((pid, identity))
                if prior is not None:
                    # reaped once, yet still holding: it outlived its reaping
                    prior["outcome"] = "unreapable"
                    continue
                outcome, _problem = self._terminate(pid, identity)
                record(holder, pid, identity,
                       {"exited": "exited", "SIGTERM": "reaped-term",
                        "SIGKILL": "reaped-kill"}.get(outcome, "unreapable"))
        else:
            for holder, pid, identity in owned_now() or ():
                prior = self._owned.get((pid, identity))
                if prior is None:
                    record(holder, pid, identity, "unreapable")
                else:
                    prior["outcome"] = "unreapable"
        problems = [f"owned detached worker {r['pid']} ({r['command']}) could "
                    "not be reaped; remedy: stop it, then rerun the teardown"
                    for r in self._owned.values()
                    if r["outcome"] == "unreapable"]
        return [dict(r) for r in self._owned.values()], problems

    # -- signals ------------------------------------------------------------
    def install_signal_handlers(self) -> None:
        for signum in self.SIGNALS:
            if signum not in self._previous_handlers:
                self._previous_handlers[signum] = signal.signal(
                    signum, self._on_signal)

    def _on_signal(self, signum, _frame) -> None:
        self.interrupted = True
        self.signals_received.append(signum)
        if self._closing:
            return  # cleanup runs once; a repeated signal never interrupts it
        raise ImageInterrupted(signum)

    def _restore_signal_handlers(self) -> None:
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError, TypeError):
                pass
        self._previous_handlers = {}

    # -- teardown -----------------------------------------------------------
    def _alive(self, pid: int, identity) -> bool:
        return identity is not None and self.start_identity(pid) == identity

    def _terminate(self, pid: int, identity) -> "tuple[str | None, str | None]":
        """SIGTERM to the process group when `pid` leads its own group, else
        to the pid; the grace; then SIGKILL. The start identity is re-checked
        before each signal, so a pid reused since it was recorded or
        classified is never signalled. Returns (outcome: "exited" | "SIGTERM"
        | "SIGKILL" | None, problem)."""
        try:
            target = -pid if os.getpgid(pid) == pid else pid
        except OSError:
            target = pid
        previous = "exited"
        for sig, wait in ((signal.SIGTERM, self.grace_s),
                          (signal.SIGKILL, self.kill_wait_s)):
            if not self._alive(pid, identity):
                return previous, None
            try:
                self.kill(target, sig)
            except ProcessLookupError:
                return previous, None
            except OSError as exc:
                return None, f"{sig.name} failed: {exc}"
            deadline = self.clock() + wait
            while self._alive(pid, identity) and self.clock() < deadline:
                self.sleep(0.2)
            if not self._alive(pid, identity):
                return sig.name, None
            previous = sig.name
        return None, "survived SIGTERM and SIGKILL"

    def _reap(self) -> "tuple[list, list]":
        reaped, problems = [], []
        for pid, identity in sorted(self._processes.items()):
            outcome, problem = self._terminate(pid, identity)
            if problem:
                problems.append(f"recorded process {pid}: {problem}")
            reaped.append({"pid": pid, "outcome": outcome or "alive"})
        return reaped, problems

    def _detach_device(self, device: str, *, force: bool) -> "tuple[int | None, str]":
        argv = ["hdiutil", "detach", *(["-force"] if force else []), device]
        try:
            proc = self._run(argv, DETACH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return None, f"timed out after {DETACH_TIMEOUT_S} s"
        return proc.returncode, (_tail(proc.stderr) or _tail(proc.stdout))

    def detach(self, record: AttachRecord) -> TeardownRecord:
        """Holder check, owned-device detach, owned-mountpoint removal."""
        td = TeardownRecord()
        root = str(self.canonical_root)
        # Step 2 for detached workers: this job's owned workers are reaped
        # before the unknown-holder rule applies (#937 SD7).
        td.owned_workers, owned_problems = self.reap_owned_workers()
        td.problems.extend(owned_problems)
        holders = None
        try:
            holders = self.holders()
            td.holders = holders
        except HolderEnumerationFailed as exc:
            td.problems.append(f"holder enumeration failed ({exc}); the volume "
                               "cannot be proven free")
        unknown = [h for h in holders or []
                   if not h.get("recorded") and not h.get("owned")]
        for holder in unknown:
            names = sorted({f.get("name") or "?" for f in holder.get("files") or []})
            td.problems.append(
                f"unknown holder pid {holder['pid']} ({holder.get('command')}) "
                f"holds {names[:3]} on the volume; it is reported and never "
                "signalled")
        owned_left = [h for h in holders or [] if h.get("owned")]
        clean = holders is not None and not unknown and not owned_left \
            and not owned_problems
        try:
            at = [m for m in self._mounts() if m["mountpoint"] == root]
        except ImageRefusal as exc:
            td.problems.append(f"mount table unreadable: {exc}")
            at = None
        if at is None:
            pass
        elif at and at[0]["device"] != record.device_node:
            td.problems.append(
                f"{at[0]['device']} is mounted at {root}, not the owned "
                f"{record.device_node}; a foreign volume is never detached")
        elif not at:
            td.detached = True
        else:
            code, message = self._detach_device(record.device_node, force=False)
            if code == 0:
                td.detached = True
            elif clean:
                code, message = self._detach_device(record.device_node, force=True)
                if code == 0:
                    td.detached = True
                else:
                    td.problems.append(f"hdiutil detach -force "
                                       f"{record.device_node} failed: {message}")
            else:
                td.problems.append(
                    f"hdiutil detach {record.device_node} refused ({message}) "
                    "and the holder check was not clean, so -force is not "
                    f"used; remedy: stop the listed holders, then run: "
                    f"hdiutil detach {record.device_node}")
        if td.detached:
            self._attached = None
            self._remove_owned_mountpoint()
        td.canonical_absent = not os.path.lexists(root)
        if not td.canonical_absent:
            td.problems.append(f"{root} still exists after teardown; remedy: "
                               f"hdiutil detach {root} if mounted, then rmdir "
                               f"{root} once empty")
        return td

    def close(self) -> TeardownRecord:
        """The teardown, once (idempotent): stop the appender, reap the
        recorded processes, then detach. Any unresolved item is a problem,
        which makes the attempt INVALID."""
        if self._closed is not None:
            return self._closed
        self._closing = True
        try:
            problems, appender_error = [], None
            if self._appender is not None:
                try:
                    self._appender()
                except BaseException as exc:  # noqa: BLE001 - invalidates
                    appender_error = f"{type(exc).__name__}: {exc}"
                    problems.append(f"the appender failed ({appender_error}); "
                                    "a dead appender invalidates the workload")
            reaped, reap_problems = self._reap()
            problems.extend(reap_problems)
            problems.extend(self._abort_problems)
            if self._attached is not None:
                td = self.detach(self._attached)
            else:
                root = str(self.canonical_root)
                # An aborted attach whose detach failed left our image there.
                td = TeardownRecord(detached=not self._abort_problems,
                                    canonical_absent=not os.path.lexists(root),
                                    owned_workers=[dict(r) for r in
                                                   self._owned.values()])
                if not td.canonical_absent:
                    # A failed or interrupted attach can leave a mount or a
                    # directory at C; it is never removed automatically.
                    td.problems.append(
                        f"{root} still exists after teardown; remedy: hdiutil "
                        f"detach {root} if mounted, then rmdir {root} once "
                        "empty")
            td.problems = problems + td.problems
            td.reaped = reaped
            td.appender_error = appender_error
            td.signals = list(self.signals_received)
            self._closed = td
            return td
        finally:
            self._restore_signal_handlers()


# ── clone, inventory, seal ─────────────────────────────────────────────────

_LIBC = None


def _libc():
    global _LIBC
    if _LIBC is None:
        if sys.platform != "darwin":
            raise OSError(errno.ENOTSUP, "clonefile(2) needs macOS")
        _LIBC = ctypes.CDLL("libc.dylib", use_errno=True)
    return _LIBC


def _band_listing(bundle: pathlib.Path) -> "list[tuple]":
    bands = pathlib.Path(bundle) / "bands"
    listing = []
    for entry in sorted(os.scandir(bands), key=lambda e: e.name):
        st = entry.stat(follow_symlinks=False)
        listing.append((entry.name, st.st_size))
    meta = []
    for name in BUNDLE_META_FILES:
        path = pathlib.Path(bundle) / name
        meta.append((name, path.stat().st_size if path.exists() else None))
    return listing + meta


def clonefile_bundle(src: pathlib.Path, dst: pathlib.Path) -> None:
    """One clonefile(2) of the detached, sealed bundle directory `src` to the
    new path `dst`. There is no copy fallback: a failed clone raises OSError,
    and a clone whose band listing differs from the source's raises."""
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    if not src.is_dir():
        raise FileNotFoundError(errno.ENOENT, "bundle to clone is missing", str(src))
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "clone destination exists", str(dst))
    clone = _libc().clonefile
    clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    clone.restype = ctypes.c_int
    rc = clone(os.fsencode(str(src)), os.fsencode(str(dst)), 0)
    if rc != 0:
        err = ctypes.get_errno() or errno.EIO
        raise OSError(err, f"clonefile({src}, {dst}) failed and there is no "
                           f"copy fallback: {os.strerror(err)}")
    before, after = _band_listing(src), _band_listing(dst)
    if before != after:
        raise RuntimeError(f"the clone {dst} lists other bands than {src}")


def corpus_provider(relpath: str) -> "str | None":
    """The provider whose cursor families track this volume-relative path:
    Claude transcripts under claude/projects/, Codex rollouts under a
    codex-*/sessions/ or codex-*/archived_sessions/ root."""
    parts = relpath.split("/")
    if not relpath.endswith(".jsonl") or len(parts) < 3:
        return None
    if parts[0] == "claude" and parts[1] == "projects":
        return "claude"
    if parts[0].startswith("codex-") and parts[1] in ("sessions",
                                                       "archived_sessions"):
        return "codex"
    return None


def image_source_roots(canonical_root, inventory: dict) -> dict:
    """{"canonical": C, "claude": [walked roots], "codex": [walked roots]}
    under C, derived from the inventory (catchup.image_manifest's `roots`)."""
    canonical = str(canonical_root).rstrip("/")
    roots = {"claude": set(), "codex": set()}
    for rel in inventory:
        provider = corpus_provider(rel)
        if provider:
            parts = rel.split("/")
            roots[provider].add(f"{canonical}/{parts[0]}/{parts[1]}")
    return {"canonical": canonical, "claude": sorted(roots["claude"]),
            "codex": sorted(roots["codex"])}


def _walk_files(root: pathlib.Path):
    """(relpath, lstat) of every regular file under `root`, housekeeping at
    the volume root excluded, sorted by path bytes. Any non-regular entry
    other than a directory raises: an inventory never skips one silently."""
    root = pathlib.Path(root)
    found = []
    for walk_root, directories, files in os.walk(root, followlinks=False):
        if pathlib.Path(walk_root) == root:
            directories[:] = [d for d in directories if d not in HOUSEKEEPING]
            files = [f for f in files if f not in HOUSEKEEPING]
        for name in directories:
            if os.path.islink(os.path.join(walk_root, name)):
                raise ValueError(f"symlink in the inventory root: "
                                 f"{os.path.join(walk_root, name)}")
        for name in files:
            path = os.path.join(walk_root, name)
            st = os.lstat(path)
            if not stat_mod.S_ISREG(st.st_mode):
                raise ValueError(f"not a regular file: {path}")
            found.append((os.path.relpath(path, root).replace(os.sep, "/"), st))
    found.sort(key=lambda item: os.fsencode(item[0]))
    return found


def source_inventory(root: pathlib.Path) -> "dict[str, list[int]]":
    """{relpath: [st_ino, size, mtime_ns]} of the attached volume."""
    return {rel: [st.st_ino, st.st_size, st.st_mtime_ns]
            for rel, st in _walk_files(root)}


def volume_device(root: pathlib.Path) -> int:
    return os.stat(root).st_dev


def inventory_record(root: pathlib.Path) -> dict:
    """The receipts' `sourceInventory`: {"stDev": int, "files": {relpath:
    [st_ino, size, mtime_ns]}}."""
    return {"stDev": volume_device(root), "files": source_inventory(root)}


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_seal(bundle: pathlib.Path, *, content: bool) -> dict:
    """The seal of a detached bundle: every band's name, size, inode and
    mtime_ns (plus its SHA-256 with `content`), and Info.plist, Info.bckup and
    token. `sealSha256` covers (name, size, SHA-256) with `content`, else
    (name, size, inode, mtime_ns), plus the three files' sizes and digests."""
    bundle = pathlib.Path(bundle)
    bands = []
    for entry in sorted(os.scandir(bundle / "bands"), key=lambda e: e.name):
        st = entry.stat(follow_symlinks=False)
        if not stat_mod.S_ISREG(st.st_mode):
            raise ValueError(f"band {entry.path} is not a regular file")
        band = {"name": entry.name, "size": st.st_size, "inode": st.st_ino,
                "mtimeNs": st.st_mtime_ns}
        if content:
            band["sha256"] = _sha256_file(entry.path)
        bands.append(band)
    files = {}
    for name in BUNDLE_META_FILES:
        st = os.lstat(bundle / name)
        if not stat_mod.S_ISREG(st.st_mode):
            raise ValueError(f"{bundle / name} is not a regular file")
        files[name] = {"size": st.st_size, "inode": st.st_ino,
                       "mtimeNs": st.st_mtime_ns,
                       "sha256": _sha256_file(bundle / name)}
    fields = ("name", "size", "sha256") if content else (
        "name", "size", "inode", "mtimeNs")
    basis = {"content": bool(content),
             "bands": [{k: b[k] for k in fields} for b in bands],
             "files": {n: {"size": f["size"], "sha256": f["sha256"]}
                       for n, f in files.items()}}
    seal = hashlib.sha256(json.dumps(basis, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
    return {"bundle": str(bundle), "content": bool(content), "bands": bands,
            "files": files, "sealSha256": seal}


def seal_metadata_problems(recorded: dict, bundle: pathlib.Path) -> "list[str]":
    """A recorded seal against the bundle as it is now, by metadata: band
    names, sizes, inodes and mtime_ns, and the three files (spec §5.4 step 1,
    §5.5 step 1)."""
    now = bundle_seal(bundle, content=False)
    problems = []
    keyed = {b["name"]: b for b in recorded.get("bands") or []}
    current = {b["name"]: b for b in now["bands"]}
    for name in sorted(set(keyed) | set(current)):
        a, b = keyed.get(name), current.get(name)
        if a is None or b is None:
            problems.append(f"band {name} is {'new' if a is None else 'missing'}")
            continue
        diff = [k for k in ("size", "inode", "mtimeNs") if a.get(k) != b.get(k)]
        if diff:
            problems.append(f"band {name} changed ({diff})")
    for name in BUNDLE_META_FILES:
        a = (recorded.get("files") or {}).get(name) or {}
        b = now["files"][name]
        diff = [k for k in ("size", "inode", "mtimeNs", "sha256")
                if a.get(k) != b.get(k)]
        if diff:
            problems.append(f"{name} changed ({diff})")
    return problems


# ── the two-sided content digest ───────────────────────────────────────────

def _content_digest(root: pathlib.Path) -> dict:
    digest = hashlib.sha256()
    count = total = 0
    for rel, before in _walk_files(root):
        path = os.path.join(root, rel)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if (opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                    before.st_ino, before.st_size, before.st_mtime_ns):
                raise RuntimeError(f"{path} changed before it was read")
            rel_bytes = os.fsencode(rel)
            digest.update(rel_bytes + b"\0" + str(opened.st_size).encode() + b"\0")
            read = 0
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                read += len(chunk)
                digest.update(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if read != before.st_size or (after.st_size, after.st_mtime_ns) != (
                before.st_size, before.st_mtime_ns):
            raise RuntimeError(f"{path} changed while it was read")
        count += 1
        total += read
    return {"sha256": digest.hexdigest(), "files": count, "bytes": total}


def source_fingerprint(source: pathlib.Path) -> str:
    """The source host's metadata pre-pass: exactly dashboard-soak.py's
    `_fixture_source_fingerprint` of a plain tree (sorted relative paths,
    each `relpath \\0 size \\0 mtime_ns \\n`), without reading file bytes."""
    source = pathlib.Path(source).expanduser().resolve()
    digest = hashlib.sha256()
    for walk_root, directories, files in os.walk(source, followlinks=False):
        directories.sort()
        for name in sorted(files):
            path = pathlib.Path(walk_root) / name
            st = path.stat()
            relative = path.relative_to(source).as_posix()
            digest.update(
                f"{relative}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def content_digest(root: pathlib.Path) -> str:
    """SHA-256 over the sorted relative paths, each framed as `relpath \\0
    decimal-length \\0 bytes`, with each file's size and mtime_ns stable
    across its read (spec §5.3 step 2). Housekeeping is excluded, so both
    hosts digest the same population."""
    return _content_digest(pathlib.Path(root))["sha256"]


# ── the budget watchdog ────────────────────────────────────────────────────

def ioreg_bytes_written(text: str, disk: str) -> int:
    """The cumulative `Statistics."Bytes (Write)"` of the IOBlockStorageDriver
    whose whole-disk media is `disk`, from `ioreg -c IOBlockStorageDriver -r
    -w0 -l` text."""
    blocks, current = [], None
    for line in _decode(text).splitlines():
        if line.startswith("+-o IOBlockStorageDriver"):
            current = []
            blocks.append(current)
        elif current is not None:
            current.append(line)
    found = []
    for block in blocks:
        stats, bsd = None, None
        for line in block:
            if bsd is None and '"BSD Name" = ' in line:
                bsd = line.split('"BSD Name" = ', 1)[1].strip().strip('"')
            if stats is None and "+-o " not in line and '"Statistics" = ' in line:
                stats = line
            if "+-o " in line and stats is None:
                stats = ""
        if bsd == disk:
            match = re.search(r'"Bytes \(Write\)"=(\d+)', stats or "")
            if match is None:
                raise RuntimeError(f"no driver Bytes (Write) for {disk}")
            found.append(int(match.group(1)))
    if len(found) != 1:
        raise RuntimeError(f"{len(found)} IOBlockStorageDriver nodes for {disk}")
    return found[0]


def disk_write_meter(volume: pathlib.Path, *, run=subprocess.run):
    """A reader of the backing physical disk's cumulative bytes written
    (F21). The disk is resolved once, from `diskutil info -plist` of the
    volume, through its single APFS physical store."""
    proc = run(["diskutil", "info", "-plist", str(volume)],
               capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"diskutil info exited {proc.returncode}: "
                           f"{_tail(proc.stderr)}")
    info = plistlib.loads(proc.stdout if isinstance(proc.stdout, bytes)
                          else proc.stdout.encode())
    stores = [s.get("APFSPhysicalStore")
              for s in info.get("APFSPhysicalStores") or []]
    if len(stores) != 1 or not isinstance(stores[0], str):
        raise RuntimeError(f"{volume} has {len(stores)} APFS physical stores; "
                           "the meter needs exactly one")
    match = re.fullmatch(r"(disk\d+)(?:s\d+)*", stores[0])
    if match is None:
        raise RuntimeError(f"unrecognized physical store {stores[0]!r}")
    disk = match.group(1)

    def read() -> int:
        out = run(["ioreg", "-c", "IOBlockStorageDriver", "-r", "-w0", "-l"],
                  capture_output=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(f"ioreg exited {out.returncode}")
        return ioreg_bytes_written(out.stdout, disk)

    read.disk = disk
    return read


def free_bytes_of(path: pathlib.Path):
    """statvfs free bytes of the volume holding `path` (the free-space
    guard; never treated as writes)."""
    def read() -> int:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    return read


def _write_json_atomic(path: pathlib.Path, payload: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    with open(tmp, "w") as fh:
        fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class BudgetWatchdog:
    """One job's write and wall-clock budget (spec §5.2, §7.4).

    The meter is cumulative bytes written; the job's consumption is its
    increase. The ledger holds every earlier job's consumption of the same
    kind, read at start under an O_EXCL lock file that the job holds until
    `stop()`, and is atomically replaced at each sample and at exit. The stop
    margin is m = RHO * (i + g) + r: from the last sample that left more than
    m to the end of teardown, at most m more bytes can be written."""

    RHO = 2.5e9
    # The full-size cap is aggregate over the working root's ledger. Measured
    # basis (#937 P0 attempt 1, 2026-11-09): the producer's rebuild wrote
    # about 105 MB/s to the backing disk, about 20x the store's net growth
    # (SQLite page rewrites, APFS inside APFS), so a full P0 rebuild is about
    # 600 GB and the campaign (P0, two P1s, the reference and the leg) about
    # 1.2 TB. The margin stays 52 GB, far below budget / 2.
    KINDS = {
        "full": {"budget": 1.5e12, "seconds": 57600, "i": 10, "g": 10,
                 "r": 2e9},
        "oracle": {"budget": 30e9, "seconds": 3600, "i": 2, "g": 3, "r": 0.5e9},
    }
    FREE_RESERVE = FREE_RESERVE_BYTES

    def __init__(self, *, kind: str, ledger: pathlib.Path, meter, free_bytes,
                 deadline_s: float, on_stop, clock=time.monotonic):
        if kind not in self.KINDS:
            raise ValueError(f"unknown watchdog kind {kind!r}")
        self.kind = kind
        self.ledger = pathlib.Path(ledger)
        self.lock_path = self.ledger.with_name(self.ledger.name + ".lock")
        self.meter = meter
        self.free_bytes = free_bytes
        self.deadline_s = float(deadline_s)
        self.on_stop = on_stop
        self.clock = clock
        self.samples: "list[dict]" = []
        self.stop_reason: "str | None" = None
        self._thread = None
        self._event = threading.Event()
        self._mutex = threading.Lock()
        self._lock_fd = None
        self._result = None
        self._started = False

    def params(self) -> dict:
        return dict(self.KINDS[self.kind])

    def margin(self) -> float:
        p = self.params()
        return self.RHO * (p["i"] + p["g"]) + p["r"]

    def _read_ledger(self) -> dict:
        try:
            ledger = json.loads(self.ledger.read_text())
        except FileNotFoundError:
            return {"schemaVersion": 1, "kind": self.kind, "consumedBytes": 0,
                    "consumedSeconds": 0.0, "jobs": []}
        if not isinstance(ledger, dict) or ledger.get("kind") != self.kind:
            raise ImageRefusal(f"ledger {self.ledger} is not a {self.kind} ledger",
                               "use the job kind's own ledger")
        return ledger

    def start(self, *, thread: bool = True) -> None:
        p, m = self.params(), self.margin()
        if m >= p["budget"] / 2:
            raise ImageRefusal(
                f"watchdog configuration error: the {self.kind} stop margin "
                f"{m:.0f} is not below half the {p['budget']:.0f}-byte budget",
                "correct the kind's i, g, r or budget")
        try:
            self._lock_fd = os.open(self.lock_path,
                                    os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            raise ImageRefusal(
                f"the ledger lock {self.lock_path} exists: another job holds "
                "the ledger, or a stopped job left the lock",
                f"confirm no job runs, then remove {self.lock_path}") from None
        try:
            os.write(self._lock_fd, f"{os.getpid()}\n".encode())
            ledger = self._read_ledger()
            self._prior_bytes = int(ledger.get("consumedBytes") or 0)
            self._prior_seconds = float(ledger.get("consumedSeconds") or 0.0)
            self._jobs = list(ledger.get("jobs") or [])
            if (self._prior_bytes >= p["budget"]
                    or self._prior_seconds >= p["seconds"]):
                raise ImageRefusal(
                    f"the {self.kind} ledger is at its cap ({self._prior_bytes} "
                    f"bytes, {self._prior_seconds:.0f} s consumed)",
                    "the session's budget is spent; record it and stop")
            if p["budget"] - self._prior_bytes < m:
                raise ImageRefusal(
                    f"the remaining {self.kind} write budget "
                    f"{p['budget'] - self._prior_bytes:.0f} is below the stop "
                    f"margin {m:.0f}", "the session's budget is spent")
            try:
                reading = int(self.meter())
            except Exception as exc:  # noqa: BLE001 - unreadable refuses
                raise ImageRefusal(f"the write meter is unreadable at start: "
                                   f"{exc}", "the cap cannot be enforced; fix "
                                   "the meter before the job") from exc
            try:
                free = int(self.free_bytes())
            except Exception as exc:  # noqa: BLE001
                raise ImageRefusal(f"free space unreadable at start: {exc}",
                                   "inspect the backing volume") from exc
            if free < self.FREE_RESERVE:
                raise ImageRefusal(f"free space {free} is below the "
                                   f"{self.FREE_RESERVE}-byte reserve",
                                   "free space on the backing volume")
        except BaseException:
            self._release_lock()
            raise
        self._t0 = self.clock()
        self._start_reading = self._last = reading
        self._started = True
        self.samples.append({"t": 0.0, "meter": reading, "free": free,
                             "consumedBytes": self._prior_bytes})
        self._write_ledger(0, 0.0, final=False)
        if thread:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="soak-budget-watchdog")
            self._thread.start()

    def _loop(self) -> None:
        while not self._event.wait(self.params()["i"]):
            if self.sample() is not None:
                return

    def _write_ledger(self, job_bytes: int, job_seconds: float, *,
                      final: bool) -> None:
        p = self.params()
        job = {"kind": self.kind, "pid": os.getpid(),
               "bytes": job_bytes, "seconds": round(job_seconds, 3),
               "stopReason": self.stop_reason, "final": final}
        _write_json_atomic(self.ledger, {
            "schemaVersion": 1, "kind": self.kind,
            "budgetBytes": p["budget"], "budgetSeconds": p["seconds"],
            "margin": self.margin(),
            "consumedBytes": self._prior_bytes + job_bytes,
            "consumedSeconds": round(self._prior_seconds + job_seconds, 3),
            "jobs": self._jobs + [job]})

    def sample(self) -> "str | None":
        """One sample; returns the stop reason when this sample stops the
        job (`on_stop(reason)` is called once)."""
        with self._mutex:
            p, m = self.params(), self.margin()
            elapsed = self.clock() - self._t0
            try:
                reading = int(self.meter())
            except Exception:  # noqa: BLE001 - unreadable stops the job
                reading = None
            try:
                free = int(self.free_bytes())
            except Exception:  # noqa: BLE001
                free = None
            record = {"t": round(elapsed, 3), "meter": reading, "free": free}
            reason = None
            if reading is None:
                reason = ("the write meter is unreadable mid-job; the cap is "
                          "unenforceable")
            elif reading < self._last:
                reason = (f"the write meter decreased ({self._last} -> "
                          f"{reading}): a counter reset")
            else:
                self._last = reading
                consumed = self._prior_bytes + (reading - self._start_reading)
                record["consumedBytes"] = consumed
                if p["budget"] - consumed < m:
                    reason = (f"the remaining write budget "
                              f"{p['budget'] - consumed:.0f} is below the stop "
                              f"margin {m:.0f}")
                elif self._prior_seconds + elapsed >= p["seconds"]:
                    reason = "the wall-clock budget is exhausted"
                elif elapsed >= self.deadline_s:
                    reason = f"the job's {self.deadline_s:.0f} s deadline passed"
            if reason is None and (free is None or free < self.FREE_RESERVE):
                reason = (f"free space {free} is below the "
                          f"{self.FREE_RESERVE}-byte cleanup reserve")
            self.samples.append(record)
            self._write_ledger(self._last - self._start_reading, elapsed,
                               final=False)
            if reason is None or self.stop_reason is not None:
                return reason if self.stop_reason is None else None
            self.stop_reason = reason
        try:
            self.on_stop(reason)
        except Exception:  # noqa: BLE001 - recorded, the stop stands
            pass
        return reason

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
            try:
                os.unlink(self.lock_path)
            except FileNotFoundError:
                pass

    def stop(self) -> dict:
        """End the job: a final reading, the ledger's final record, the lock
        released. Returns the samples and totals, overshoot included."""
        if self._result is not None:
            return self._result
        if not self._started:
            raise RuntimeError("the watchdog was never started")
        self._event.set()
        if self._thread is not None:
            self._thread.join(30)
        p = self.params()
        with self._mutex:
            elapsed = self.clock() - self._t0
            try:
                final = int(self.meter())
                readable = final >= self._last
            except Exception:  # noqa: BLE001
                final, readable = None, False
            if readable:
                self._last = final
            self.samples.append({"t": round(elapsed, 3), "meter": final,
                                 "final": True})
            job_bytes = self._last - self._start_reading
            consumed = self._prior_bytes + job_bytes
            self._write_ledger(job_bytes, elapsed, final=True)
        self._release_lock()
        self._result = {
            "kind": self.kind, "budgetBytes": p["budget"],
            "budgetSeconds": p["seconds"], "rho": self.RHO, "i": p["i"],
            "g": p["g"], "r": p["r"], "margin": self.margin(),
            "startReading": self._start_reading, "samples": list(self.samples),
            "stopReason": self.stop_reason, "jobBytes": job_bytes,
            "jobSeconds": round(elapsed, 3), "consumedBytes": consumed,
            "consumedSeconds": round(self._prior_seconds + elapsed, 3),
            "overshootBytes": max(0, consumed - p["budget"]),
            "finalMeterReadable": readable}
        return self._result


# ── the runner host lock ───────────────────────────────────────────────────

def host_lock_opener(lock_path=None):
    """The runner's own `open_host_lock` (bin/_lib_runner_host.py), loaded by
    path from this checkout; it never creates the lock inode."""
    def opener():
        bindir = str(REPO / "bin")
        if bindir not in sys.path:
            sys.path.insert(0, bindir)
        # Waived (#1116): runner tooling the mirror leaves out: on a public
        # clone the import fails and acquire_host_lock refuses at once, with no
        # retry and no lock inode created.
        module = importlib.import_module(
            "_lib_runner_host")  # mirror-private-ok: runner tooling the mirror leaves out
        if lock_path is None:
            return module.open_host_lock()
        return module.open_host_lock(str(lock_path))
    return opener


def acquire_host_lock(*, wait_s: float = 1800, retry_s: float = 30,
                      opener=None, sleep=time.sleep) -> int:
    """The runner host lock's descriptor (spec §5.7), retried every `retry_s`
    for at most `wait_s`, then refused. A missing inode refuses at once."""
    opener = opener or host_lock_opener()
    waited, attempts = 0.0, 0
    while True:
        attempts += 1
        try:
            fd = opener()
        except Exception as exc:  # noqa: BLE001 - the runner's refusal
            raise ImageRefusal(
                f"the runner host lock cannot be taken: {exc}",
                "the lock inode belongs to the runner installation; reinstall "
                "the runner tooling, never create it by hand") from exc
        if fd is not None:
            return fd
        if waited + retry_s > wait_s:
            raise ImageRefusal(
                f"the runner host lock is still held after {waited:.0f} s "
                f"({attempts} attempts)",
                "wait for the running job to finish, then retry")
        sleep(retry_s)
        waited += retry_s


# ── P0 equivalence ─────────────────────────────────────────────────────────

def _open_ro(path: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro",
                           uri=True)


def _table_columns(conn, table: str) -> "list[str] | None":
    if conn is None:
        return None
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = ?", (table,)).fetchone():
        return None
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _order(value):
    if value is None:
        return (0, 0)
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    return (3, bytes(value))


def _row_order(row) -> tuple:
    return tuple(_order(v) for v in row)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class StoreEquivalence:
    """Spec §5.3 step 7 (and step 6 with an inventory): the retained stores
    against the producer's normalized ones, over 100% of both sides.

    Byte-derived families compare as ordered multisets keyed by natural key;
    a compared difference, a missing key or an extra key refuses, an
    `account_key`-only difference is reported. Carriers are judged by their
    producer-specific invariants. Omitted families are not read. With
    `inventory` ({relpath: [st_ino, size, mtime_ns]} of the volume at
    `canonical_root`) the normalized cursor population must be exactly the
    corpus, each identity-bearing row matching its file. `file_key(root_key,
    path)`, when given, names the path of an advanced Codex incarnation."""

    #: (store, table, natural key, compared (None: every other column),
    #:  excluded, reported)
    FAMILIES = (
        ("cache.db", "session_files", ("path",),
         ("size_bytes", "mtime_ns", "last_byte_offset", "session_id",
          "project_path"), ("last_ingested_at",), ("account_key",)),
        ("cache.db", "session_entries", ("source_path", "line_offset"), None,
         ("id", "mutation_seq", "mutation_min_ts"), ("account_key",)),
        ("cache.db", "codex_session_files", ("path",), None,
         ("last_ingested_at", "device_id", "inode"), ("account_key",)),
        ("cache.db", "codex_session_entries", ("source_path", "line_offset"),
         None, ("id",), ("account_key",)),
        ("cache.db", "quota_window_snapshots",
         ("source_path", "line_offset", "observed_slot", "logical_limit_key"),
         None, ("id",), ("account_key",)),
        ("conversations.db", "conversation_source_files", ("path",),
         ("size_bytes", "mtime_ns", "last_byte_offset",
          "committed_prefix_sha256"),
         ("last_ingested_at", "device_id", "inode", "source_incarnation_id"), ()),
        ("conversations.db", "conversation_messages",
         ("source_path", "byte_offset"), None, ("id",), ("account_key",)),
        ("conversations.db", "codex_conversation_source_files", ("path",), None,
         ("last_ingested_at", "device_id", "inode"), ()),
        ("conversations.db", "codex_conversation_events",
         ("source_path", "line_offset"), None, ("id",), ("account_key",)),
        ("conversations.db", "codex_conversation_messages",
         ("source_path", "line_offset", "kind", "event_type"), None, ("id",),
         ("account_key",)),
        # A live cache.db table in the producer (4892abbec dropped it from
        # conversations.db); its two clocks are stamped at ingest.
        ("cache.db", "codex_conversation_threads",
         ("conversation_key",), None, ("first_seen_utc", "last_seen_utc"), ()),
    )
    #: cache.db tables the legacy-empty check's name pattern matches but the
    #: producer keeps live there (compared as a family instead).
    LIVE_CACHE_TABLES = ("codex_conversation_threads",)
    OMITTED = {
        "conversation_sessions": "aggregate of compared rows plus clocks and "
                                 "revision counters",
        "codex_conversation_rollups": "aggregate of compared rows plus clocks",
        "*_staging": "staging twins of the aggregates",
        "conversation_file_touches": "keyed by surrogate message ids; derived "
                                     "from compared blocks_json",
        "codex_conversation_file_touches": "keyed by surrogate ids; derived "
                                           "from compared detail_json/text",
        "codex_find_projection": "keyed by surrogate ids; derived",
        "conversation_ai_titles": "re-derived from compared message rows",
        "codex_accounting_change_log": "mutation-ordered log",
        "quota_window_change_log": "mutation-ordered log",
        "cache_meta": "compared key by key against P0's step-5 expectation",
    }

    def __init__(self, *, canonical_root=None, inventory: "dict | None" = None,
                 file_key=None):
        self.canonical_root = (None if canonical_root is None
                               else str(canonical_root).rstrip("/"))
        self.inventory = inventory
        self.file_key = file_key
        if inventory is not None and self.canonical_root is None:
            raise ValueError("an inventory needs its canonical root")

    def compare(self, retained: pathlib.Path, normalized: pathlib.Path) -> dict:
        retained, normalized = pathlib.Path(retained), pathlib.Path(normalized)
        conns = {}
        try:
            for side, directory in (("retained", retained),
                                    ("normalized", normalized)):
                for store in ("cache.db", "conversations.db"):
                    path = directory / store
                    conns[(side, store)] = _open_ro(path) if path.exists() else None
            problems, reported, tables = [], {}, {}
            for family in self.FAMILIES:
                self._family(conns, family, problems, reported, tables)
            self._carriers(conns, problems, reported, tables)
            self._legacy_empty(conns, problems, tables)
            if self.inventory is not None:
                self._population(conns, problems, tables)
            return {"problems": problems, "reported": reported,
                    "tables": tables, "omitted": dict(self.OMITTED)}
        finally:
            for conn in conns.values():
                if conn is not None:
                    conn.close()

    # -- families -----------------------------------------------------------
    @staticmethod
    def _groups(conn, table, key, columns):
        select = ", ".join(_quote(c) for c in (*key, *columns))
        order = ", ".join(f"{_quote(c)} COLLATE BINARY" for c in key)
        current, rows = None, []
        for row in conn.execute(f"SELECT {select} FROM {_quote(table)} "
                                f"ORDER BY {order}"):
            k = tuple(row[:len(key)])
            if rows and k != current:
                yield current, rows
                rows = []
            current = k
            rows.append(tuple(row[len(key):]))
        if rows:
            yield current, rows

    def _family(self, conns, family, problems, reported, tables) -> None:
        store, table, key, compared, excluded, report = family
        name = f"{store}.{table}"
        a_cols = _table_columns(conns[("retained", store)], table)
        b_cols = _table_columns(conns[("normalized", store)], table)
        if a_cols is None and b_cols is None:
            tables[name] = {"present": False}
            return
        if a_cols is None or b_cols is None:
            problems.append(f"{name}: present on only one side "
                            f"({'normalized' if a_cols is None else 'retained'})")
            return
        if set(a_cols) != set(b_cols):
            problems.append(f"{name}: columns differ (retained only "
                            f"{sorted(set(a_cols) - set(b_cols))}, normalized "
                            f"only {sorted(set(b_cols) - set(a_cols))})")
            return
        if any(k not in a_cols for k in key):
            problems.append(f"{name}: natural key {key} is not in the table")
            return
        if compared is None:
            cmp_cols = [c for c in a_cols
                        if c not in key and c not in excluded and c not in report]
        else:
            cmp_cols = [c for c in compared if c in a_cols]
            # The real columns, from PRAGMA table_info on both sides (#937
            # F10): one the comparator does not classify fails closed and is
            # named, never skipped.
            unclassified = [c for c in a_cols if c not in key
                            and c not in compared and c not in excluded
                            and c not in report]
            if unclassified:
                problems.append(f"{name}: column(s) {unclassified} are not "
                                "classified by the comparator (compared, "
                                "excluded or reported); refused, not skipped")
        rep_cols = [c for c in report if c in a_cols]
        width = len(cmp_cols)
        counts = {"retained": 0, "normalized": 0}
        missing, extra, different, rep_diff = [], [], [], []
        n_missing = n_extra = n_different = n_rep = 0
        a_iter = self._groups(conns[("retained", store)], table, key,
                              cmp_cols + rep_cols)
        b_iter = self._groups(conns[("normalized", store)], table, key,
                              cmp_cols + rep_cols)
        end = object()
        a, b = next(a_iter, end), next(b_iter, end)
        while a is not end or b is not end:
            if b is end or (a is not end and _row_order(a[0]) < _row_order(b[0])):
                counts["retained"] += len(a[1])
                n_missing += 1
                if len(missing) < _FIRST:
                    missing.append(list(a[0]))
                a = next(a_iter, end)
                continue
            if a is end or _row_order(b[0]) < _row_order(a[0]):
                counts["normalized"] += len(b[1])
                n_extra += 1
                if len(extra) < _FIRST:
                    extra.append(list(b[0]))
                b = next(b_iter, end)
                continue
            counts["retained"] += len(a[1])
            counts["normalized"] += len(b[1])
            ra = sorted(a[1], key=_row_order)
            rb = sorted(b[1], key=_row_order)
            if [r[:width] for r in ra] != [r[:width] for r in rb]:
                n_different += 1
                if len(different) < _FIRST:
                    different.append(list(a[0]))
            elif [r[width:] for r in ra] != [r[width:] for r in rb]:
                n_rep += 1
                if len(rep_diff) < _FIRST:
                    rep_diff.append(list(a[0]))
            a, b = next(a_iter, end), next(b_iter, end)
        tables[name] = {"present": True, "key": list(key), "compared": cmp_cols,
                        "excluded": [c for c in excluded if c in a_cols],
                        "reported": rep_cols, "rows": counts,
                        "missingKeys": n_missing, "extraKeys": n_extra,
                        "differentKeys": n_different}
        for count, first, label in ((n_missing, missing, "missing from the "
                                     "normalized store"),
                                    (n_extra, extra, "extra in the normalized "
                                     "store"),
                                    (n_different, different, "differ in "
                                     f"compared columns {cmp_cols}")):
            if count:
                problems.append(f"{name}: {count} key(s) {label}; first "
                                f"{first}")
        if rep_cols:
            reported[name] = {"columns": rep_cols, "differentKeys": n_rep,
                              "first": rep_diff}

    # -- carriers -------------------------------------------------------------
    @staticmethod
    def _rows(conn, table, columns) -> "list[tuple] | None":
        have = _table_columns(conn, table)
        if have is None:
            return None
        missing = [c for c in columns if c not in have]
        if missing:
            raise sqlite3.OperationalError(f"{table} lacks {missing}")
        return conn.execute(f"SELECT {', '.join(_quote(c) for c in columns)} "
                            f"FROM {_quote(table)}").fetchall()

    def _carrier_rows(self, conns, store, table, columns, problems, tables,
                      *, excluded=(), all_but=None):
        """(retained rows, normalized rows) of `columns`, or None. The real
        columns come from PRAGMA table_info on both sides (#937 F10): the
        sides must have the same columns, and a column that is neither read
        (`columns`) nor `excluded` is named as unclassified, a refusal.
        `all_but` reads every real column except those."""
        name = f"{store}.{table}"
        try:
            have = [_table_columns(conns[(side, store)], table)
                    for side in ("retained", "normalized")]
        except sqlite3.Error as exc:
            problems.append(f"{name}: unreadable carrier: {exc}")
            return None
        if have[0] is None and have[1] is None:
            tables[name] = {"present": False}
            return None
        if have[0] is None or have[1] is None:
            problems.append(f"{name}: carrier present on only one side")
            return None
        if set(have[0]) != set(have[1]):
            problems.append(f"{name}: columns differ (retained only "
                            f"{sorted(set(have[0]) - set(have[1]))}, "
                            f"normalized only "
                            f"{sorted(set(have[1]) - set(have[0]))})")
            return None
        if all_but is not None:
            columns = tuple(c for c in have[0] if c not in all_but)
            excluded = tuple(all_but)
        unclassified = [c for c in have[0] if c not in columns
                        and c not in excluded]
        if unclassified:
            problems.append(f"{name}: column(s) {unclassified} are not "
                            "classified by the comparator (compared or "
                            "excluded); refused, not skipped")
        try:
            a = self._rows(conns[("retained", store)], table, columns)
            b = self._rows(conns[("normalized", store)], table, columns)
        except sqlite3.Error as exc:
            problems.append(f"{name}: unreadable carrier: {exc}")
            return None
        if a is None and b is None:
            tables[name] = {"present": False}
            return None
        if a is None or b is None:
            problems.append(f"{name}: carrier present on only one side")
            return None
        tables[name] = {"present": True, "columns": list(columns),
                        "excluded": list(excluded),
                        "rows": {"retained": len(a), "normalized": len(b)}}
        return a, b

    def _carriers(self, conns, problems, reported, tables) -> None:
        # codex_source_roots: the (key, canonical path) set; clocks excluded.
        got = self._carrier_rows(
            conns, "cache.db", "codex_source_roots",
            ("source_root_key", "canonical_root_path", "first_seen_utc",
             "last_seen_utc"), problems, tables)
        if got:
            a, b = got
            sa, sb = {r[:2] for r in a}, {r[:2] for r in b}
            if sa != sb:
                problems.append(f"cache.db.codex_source_roots: (key, canonical "
                                f"path) sets differ; retained only "
                                f"{sorted(sa - sb)[:_FIRST]}, normalized only "
                                f"{sorted(sb - sa)[:_FIRST]}")
            reported["cache.db.codex_source_roots"] = {
                "clockDifferences": len(set(a) ^ set(b)) // 2}
        # codex_file_incarnations: same identities; never lower; advances
        # recorded.
        advanced = {}
        got = self._carrier_rows(
            conns, "cache.db", "codex_file_incarnations",
            ("file_identity", "incarnation"), problems, tables,
            excluded=("updated_at_utc",))
        retained_inc = {}
        if got:
            a, b = got
            retained_inc, now = dict(a), dict(b)
            if set(retained_inc) != set(now):
                problems.append(
                    "cache.db.codex_file_incarnations: file identity sets "
                    f"differ; retained only "
                    f"{sorted(set(retained_inc) - set(now))[:_FIRST]}, "
                    f"normalized only {sorted(set(now) - set(retained_inc))[:_FIRST]}")
            paths = self._identity_paths(conns)
            for identity in sorted(set(retained_inc) & set(now)):
                before, after = retained_inc[identity], now[identity]
                if after < before:
                    problems.append(f"cache.db.codex_file_incarnations: "
                                    f"{identity} incarnation lowered "
                                    f"({before} -> {after})")
                elif after > before:
                    advanced[identity] = {"fileIdentity": identity,
                                          "from": before, "to": after,
                                          "path": paths.get(identity)}
            reported["cache.db.codex_file_incarnations"] = {
                "advanced": len(advanced),
                "advances": [advanced[k] for k in sorted(advanced)]}
        # codex_file_accounts: rows of an incarnation that did not advance are
        # kept exactly; new rows only for advanced incarnations.
        got = self._carrier_rows(
            conns, "cache.db", "codex_file_accounts",
            ("file_identity", "incarnation", "from_offset", "root_scope",
             "account_key"), problems, tables, excluded=("decided_at_utc",))
        if got:
            a, b = got
            sa, sb = set(a), set(b)
            lost = sorted((r for r in sa - sb if r[0] not in advanced),
                          key=_row_order)
            new = sorted(sb - sa, key=_row_order)
            foreign = [r for r in new if r[0] not in advanced
                       or r[1] <= retained_inc.get(r[0], r[1] - 1)]
            if lost:
                problems.append(f"cache.db.codex_file_accounts: {len(lost)} "
                                "decision(s) of a retained incarnation changed "
                                f"or vanished; first {lost[:_FIRST]}")
            if foreign:
                problems.append(f"cache.db.codex_file_accounts: {len(foreign)} "
                                "new decision(s) outside an advanced "
                                f"incarnation; first {foreign[:_FIRST]}")
            reported["cache.db.codex_file_accounts"] = {
                "advancedIncarnationRows": len(new) - len(foreign),
                "accountKeys": sorted({str(r[4]) for r in new
                                       if r not in foreign})[:_FIRST]}
        # codex_window_attributions: every real column but the assertion
        # clock (spec §5.3 step 7: "every column except clocks").
        got = self._carrier_rows(conns, "cache.db", "codex_window_attributions",
                                 (), problems, tables,
                                 all_but=("asserted_at_utc",))
        if got:
            a, b = got
            columns = tables["cache.db.codex_window_attributions"]["columns"]
            op = columns.index("op_id") if "op_id" in columns else 0
            if sorted(a, key=_row_order) != sorted(b, key=_row_order):
                diff = sorted({r[op] for r in set(a) ^ set(b)}, key=_order)
                problems.append(f"cache.db.codex_window_attributions: "
                                f"{len(diff)} assertion(s) differ; first "
                                f"{diff[:_FIRST]}")
        # claude_conversation_account_stamps: per retained key, exactly one
        # NEW stamp (an incarnation the retained key does not have) with the
        # same record_sha256. The producer's rebuild keeps the retained
        # incarnation's stamps (observed on the full-size corpus, 2026-11-09:
        # every one of 1,794,181 keys held its retained stamp plus one new
        # one), so a carried stamp must equal its retained row exactly.
        got = self._carrier_rows(
            conns, "conversations.db", "claude_conversation_account_stamps",
            ("canonical_source_path", "byte_offset", "record_sha256",
             "account_key", "source_incarnation_id"), problems, tables)
        if got:
            a, b = got
            before, after = {}, {}
            for side, rows in ((before, a), (after, b)):
                for path, offset, sha, account, inc in rows:
                    side.setdefault((path, offset), {})[inc] = (sha, account)
            missing = sorted(set(before) - set(after), key=_row_order)
            extra = sorted(set(after) - set(before), key=_row_order)
            changed, carried_changed, accounts, carried = [], [], 0, 0
            for key in sorted(set(before) & set(after), key=_row_order):
                old, now = before[key], after[key]
                shas = {sha for sha, _acc in old.values()}
                fresh = {inc: v for inc, v in now.items() if inc not in old}
                kept = {inc: v for inc, v in now.items() if inc in old}
                carried += len(kept)
                if any(old[inc] != v for inc, v in kept.items()):
                    carried_changed.append(list(key))
                if len(fresh) != 1 or len(shas) != 1 \
                        or next(iter(fresh.values()))[0] not in shas:
                    changed.append(list(key))
                elif next(iter(fresh.values()))[1] not in {
                        acc for _s, acc in old.values()}:
                    accounts += 1
            for count, first, label in (
                    (len(missing), missing, "retained key(s) without a new stamp"),
                    (len(changed), changed, "key(s) without exactly one new "
                     "stamp of the retained record_sha256"),
                    (len(carried_changed), carried_changed,
                     "key(s) whose carried retained stamp changed")):
                if count:
                    problems.append(f"conversations.db.claude_conversation_"
                                    f"account_stamps: {count} {label}; first "
                                    f"{[list(k) for k in first[:_FIRST]]}")
            # Spec §5.3 step 7 binds every RETAINED key to exactly one new
            # stamp with its record_sha256; a stamp for a key the retained
            # store lacks is outside that invariant, so it is reported (#937
            # F10), not refused.
            reported["conversations.db.claude_conversation_account_stamps"] = {
                "accountKeyDifferences": accounts,
                "carriedRetainedStamps": carried,
                "extraKeys": len(extra),
                "firstExtraKeys": [list(k) for k in extra[:_FIRST]]}
        # claude_conversation_account_stamp_gaps: equal key sets.
        got = self._carrier_rows(
            conns, "conversations.db", "claude_conversation_account_stamp_gaps",
            ("source_path", "byte_offset", "cause", "account_key"),
            problems, tables)
        if got:
            a, b = got
            ka, kb = {r[:2] for r in a}, {r[:2] for r in b}
            if ka != kb:
                problems.append(
                    "conversations.db.claude_conversation_account_stamp_gaps: "
                    f"key sets differ; retained only "
                    f"{sorted(ka - kb, key=_row_order)[:_FIRST]}, normalized "
                    f"only {sorted(kb - ka, key=_row_order)[:_FIRST]}")
            reported["conversations.db.claude_conversation_account_stamp_gaps"] \
                = {"causeOrAccountDifferences":
                   len({r for r in set(a) ^ set(b) if r[:2] in ka & kb}) // 2}

    def _identity_paths(self, conns) -> dict:
        if self.file_key is None:
            return {}
        conn = conns[("normalized", "cache.db")]
        have = _table_columns(conn, "codex_session_files") or []
        if "source_root_key" not in have:
            return {}
        out = {}
        for path, root_key in conn.execute(
                "SELECT path, source_root_key FROM codex_session_files"):
            try:
                out[self.file_key(root_key, path)] = path
            except Exception:  # noqa: BLE001 - a path is evidence only
                continue
        return out

    # -- legacy tables and the cursor population ----------------------------
    def _legacy_empty(self, conns, problems, tables) -> None:
        for side in ("retained", "normalized"):
            conn = conns[(side, "cache.db")]
            if conn is None:
                continue
            for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND "
                    "(name LIKE 'conversation\\_%' ESCAPE '\\' OR name LIKE "
                    "'codex\\_conversation\\_%' ESCAPE '\\') AND name NOT LIKE "
                    "'%fts%' AND sql NOT LIKE 'CREATE VIRTUAL%'").fetchall():
                if name in self.LIVE_CACHE_TABLES:
                    continue
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0]
                tables.setdefault("cache.db legacy conversation tables",
                                  {})[f"{side}:{name}"] = count
                if count:
                    problems.append(f"cache.db.{name} ({side}) holds {count} "
                                    "row(s); the legacy conversation tables "
                                    "must be empty")

    def _population(self, conns, problems, tables) -> None:
        canonical = self.canonical_root
        corpus = {}
        for rel, values in self.inventory.items():
            provider = corpus_provider(rel)
            if provider:
                ino, size, mtime = (int(v) for v in list(values)[:3])
                corpus[f"{canonical}/{rel}"] = (provider, ino, size, mtime)
        summary = {"corpusFiles": len(corpus)}
        for provider, families in CURSOR_FAMILIES.items():
            expected = {p for p, v in corpus.items() if v[0] == provider}
            for store, table in families:
                conn = conns[("normalized", store)]
                have = _table_columns(conn, table)
                name = f"{store}.{table}"
                if have is None:
                    problems.append(f"{name}: the normalized store has no "
                                    "cursor table")
                    continue
                cols = [c for c in ("path", "size_bytes", "mtime_ns",
                                    "last_byte_offset", "inode",
                                    "ingest_complete") if c in have]
                rows = {r[0]: dict(zip(cols, r)) for r in conn.execute(
                    f"SELECT {', '.join(cols)} FROM {_quote(table)}")}
                foreign = sorted(set(rows) - expected)
                missing = sorted(expected - set(rows))
                wrong = []
                for path in sorted(set(rows) & expected):
                    row, (_prov, ino, size, mtime) = rows[path], corpus[path]
                    bad = []
                    if not (row.get("last_byte_offset") == row.get("size_bytes")
                            == size):
                        bad.append("offset/size")
                    if table in IDENTITY_FAMILIES:
                        if not _under(path, canonical) or \
                                os.path.realpath(path) != path:
                            bad.append("realpath")
                        if row.get("mtime_ns") != mtime:
                            bad.append("mtime_ns")
                        if row.get("inode") != ino:
                            bad.append("inode")
                        if "ingest_complete" in row and row["ingest_complete"] != 1:
                            bad.append("ingest_complete")
                    if bad:
                        wrong.append(f"{path} ({', '.join(bad)})")
                summary[name] = {"rows": len(rows), "foreign": len(foreign),
                                 "missing": len(missing), "mismatched": len(wrong)}
                for count, first, label in (
                        (len(foreign), foreign, "cursor row(s) for a path "
                         "outside the corpus"),
                        (len(missing), missing, "corpus file(s) without a "
                         "cursor row"),
                        (len(wrong), wrong, "cursor row(s) that do not match "
                         "their file")):
                    if count:
                        problems.append(f"{name}: {count} {label}; first "
                                        f"{first[:_FIRST]}")
        tables["cursorPopulation"] = summary


# ── P2: identity equality and the image-mode admission inputs ──────────────

def _catchup():
    path = HERE / "write-attribution" / "catchup.py"
    loader = importlib.machinery.SourceFileLoader("soak_image_catchup", str(path))
    spec = importlib.util.spec_from_loader("soak_image_catchup", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["soak_image_catchup"] = module
    loader.exec_module(module)
    return module


def manifest_bytes(manifest: dict) -> bytes:
    """The exact bytes of a written image manifest (its digest's input)."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def _carrier(rel: str) -> bool:
    parts = rel.split("/")
    return parts[-1] in JOURNAL_CARRIERS or (
        len(parts) >= 2 and parts[-2] == "data" and parts[-1] in STORE_FILES)


def identity_equality(attached: AttachRecord, inventory: dict,
                      p1_receipt: dict) -> dict:
    """Spec §5.5 step 2: the attached volume against the P1 receipt's sealed
    `sourceInventory` ({"stDev", "files"}). A different device, any file's
    inode, size or mtime_ns, and the journal sidecars and store files are all
    problems; a device mismatch comes first and is flagged so the arm refuses
    before any product process. The manifest catchup's image mode reads is
    synthesized from this inventory; `manifestSha256` is the digest of its
    `manifest_bytes`."""
    problems = []
    sealed = (p1_receipt or {}).get("sourceInventory") or {}
    sealed_dev, sealed_files = sealed.get("stDev"), sealed.get("files")
    device_mismatch = False
    current = {rel: [int(v) for v in list(values)[:3]]
               for rel, values in inventory.items()}
    if not isinstance(sealed_dev, int) or not isinstance(sealed_files, dict):
        problems.append("the P1 receipt carries no sealed source inventory "
                        "(sourceInventory.stDev and .files)")
    else:
        sealed_files = {rel: [int(v) for v in list(values)[:3]]
                        for rel, values in sealed_files.items()}
        if attached.st_dev != sealed_dev:
            device_mismatch = True
            problems.append(
                f"device mismatch: the attached volume is st_dev "
                f"{attached.st_dev}, the P1 seal is {sealed_dev}; remedy: "
                "another image or volume took the device number; detach it "
                "and retry")
        missing = sorted(set(sealed_files) - set(current))
        extra = sorted(set(current) - set(sealed_files))
        changed = sorted(rel for rel in set(sealed_files) & set(current)
                         if sealed_files[rel] != current[rel])
        for rel in sorted({*missing, *extra, *changed}, key=os.fsencode):
            if _carrier(rel):
                problems.append(
                    f"carrier {rel}: sealed {sealed_files.get(rel)}, attached "
                    f"{current.get(rel)} (st_ino, size, mtime_ns)")
        for label, items in (("missing", missing), ("new", extra),
                             ("changed (st_ino, size or mtime_ns)", changed)):
            items = [rel for rel in items if not _carrier(rel)]
            if items:
                problems.append(f"{len(items)} file(s) {label} against the P1 "
                                f"inventory; first {items[:_FIRST]}")
    catchup = _catchup()
    manifest = catchup.image_manifest(
        current, image_source_roots(attached.mountpoint, current))
    return {"passed": not problems, "problems": problems,
            "deviceMismatch": device_mismatch,
            "manifestSha256": hashlib.sha256(manifest_bytes(manifest)).hexdigest(),
            "manifest": manifest}


def _write_new(path: pathlib.Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_admission_inputs(directory: pathlib.Path, equality: dict) -> pathlib.Path:
    """Write `p2-manifest.json` and the sibling `identity-receipt.json` that
    catchup's image mode reads (`WRITE_ATTRIBUTION_INPUTS=image:<manifest>`),
    as new files; returns the manifest path."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    blob = manifest_bytes(equality["manifest"])
    if hashlib.sha256(blob).hexdigest() != equality["manifestSha256"]:
        raise ValueError("the manifest no longer matches its recorded digest")
    manifest_path = directory / "p2-manifest.json"
    _write_new(manifest_path, blob)
    _write_new(directory / "identity-receipt.json", (json.dumps({
        "schemaVersion": 1, "passed": bool(equality["passed"]),
        "problems": list(equality["problems"]),
        "deviceMismatch": bool(equality.get("deviceMismatch")),
        "manifestSha256": equality["manifestSha256"],
    }, indent=2, sort_keys=True) + "\n").encode())
    return manifest_path


def main(argv=None) -> int:
    """The source host's read-only P0 pre-pass: `digest <dir>` (the two-sided
    content digest) and `fingerprint <dir>` (the metadata fingerprint)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in ("digest", "fingerprint"):
        print("usage: python3 -I soak_image.py {digest|fingerprint} <dir>",
              file=sys.stderr)
        return 2
    try:
        if not pathlib.Path(argv[1]).is_dir():
            raise NotADirectoryError(f"{argv[1]} is not a directory")
        if argv[0] == "fingerprint":
            result = {"fingerprint": source_fingerprint(pathlib.Path(argv[1]))}
        else:
            result = _content_digest(pathlib.Path(argv[1]))
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"root": argv[1], **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
