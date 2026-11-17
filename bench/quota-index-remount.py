#!/usr/bin/env python3
"""Disk-image acceptance for the Codex quota index identity (#1080).

macOS only (``hdiutil``, ``getattrlist``). Manually invoked; not part of any
test run.

    python3 bench/quota-index-remount.py --repo <checkout> --sha <commit>
                                         --work <new dir> --label <short tag>

``--work`` must not exist; it is created on the volume that will hold the
image bands (an external volume for the #1080 evidence). The product tree is
``git archive <sha>`` of ``--repo``, extracted under ``--work``, so the
receipt's SHA is the tree that ran. Every image is created fresh, mounted
under /Volumes with a unique name and detached at the end. One JSON receipt
goes to stdout; ``checks`` holds the pass/fail booleans.

Stores are isolated: CCTALLY_DATA_DIR, CLAUDE_CONFIG_DIR, CODEX_HOME and HOME
all point into the images.

Phases
  R  Remount control on image A: sync-1; device-only remount (a spacer image
     takes A's device number); append one fresh Codex quota line; sync-2;
     remount back; sync-3. A sync CONSUMES the index when the keys file keeps
     its identity, its old bytes stay, it grows by exactly the injected
     line's keys, the rebuild state is untouched, and the journal gains
     exactly the injected line's observations once each. The expected keys
     are fixed BEFORE sync-2 from the injected line's path and offset and
     the logical limit keys the journal already holds for the copied
     source line, so the oracle never reads what sync-2 wrote.
  N  Negative controls; each must leave the index INVALID:
     copiedStore (same volume), differentVolume (file copy to image B),
     sameInodeCrossVolume (image C: the keys file and every covered segment
     at A's recorded inode numbers and sizes, A's sidecar, other keys),
     sameInodeSameBirthCrossVolume (image D: the same, with A's birth times
     forced through utime), each first with A attached (devices differ) and
     then attached alone so it takes A's recorded device number
     (deviceReuse*), plus replacedKeys and replacedSegment on A.
  M  For every state the receipt gives the product's verdict and the verdict
     of each candidate identity rule evaluated on the same stats:
     devIno (the baseline), inodeOnly, orRule (inode and (device or birth)),
     strictBirth (inode and birth, device only without births) and volume
     (inode, birth when both known, volume UUID when both known, else
     device). On a baseline tree the product verdict must equal devIno.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import plistlib
import shutil
import subprocess
import sys
import time

PY = sys.executable
KEYS = ".quota-observation-keys"
META = ".quota-observation-keys.meta"


def run(argv, *, env=None, timeout=600, check=True, stdin=None):
    done = subprocess.run(argv, env=env, capture_output=True, timeout=timeout,
                          input=stdin)
    if check and done.returncode != 0:
        raise RuntimeError(f"{argv[:3]} exited {done.returncode}: "
                           f"{done.stderr.decode(errors='replace')[-2000:]}")
    return done


class Images:
    def __init__(self, work: pathlib.Path, label: str):
        self.work = work
        self.label = label
        self.attached = {}

    def volname(self, name):
        return f"q1080{self.label}{name}"

    def create(self, name, size="512m"):
        path = self.work / f"{name}.sparsebundle"
        run(["hdiutil", "create", "-quiet", "-size", size, "-type",
             "SPARSEBUNDLE", "-fs", "APFS", "-volname", self.volname(name),
             str(path)])

    def attach(self, name):
        done = run(["hdiutil", "attach", "-plist", "-nobrowse", "-noautoopen",
                    str(self.work / f"{name}.sparsebundle")])
        info = plistlib.loads(done.stdout)
        mounts = [e for e in info["system-entities"] if "mount-point" in e]
        mount = pathlib.Path(mounts[0]["mount-point"]) if mounts else None
        if len(mounts) != 1 or mount.name != self.volname(name):
            # Detach what this attach created (the whole-disk entry) before
            # refusing, so no image outlives the driver.
            devices = sorted((e["dev-entry"] for e in info["system-entities"]
                              if "dev-entry" in e), key=len)
            if devices:
                run(["hdiutil", "detach", "-force", devices[0]], check=False)
            raise RuntimeError(f"attach {name}: {mounts}")
        self.attached[name] = (mounts[0]["dev-entry"], mount)
        (mount / ".metadata_never_index").touch()
        return mount

    def detach(self, name, *, quiesce_s=180):
        device, mount = self.attached.pop(name)
        deadline = time.monotonic() + quiesce_s
        holders = ""
        while time.monotonic() < deadline:
            holders = run(["lsof", "-t", str(mount)],
                          check=False).stdout.decode().strip()
            if not holders:
                break
            time.sleep(2)
        for _ in range(5):
            if run(["hdiutil", "detach", device], check=False).returncode == 0:
                return {"holders": holders or None, "forced": False}
            time.sleep(3)
        run(["hdiutil", "detach", "-force", device])
        return {"holders": holders or None, "forced": True}

    def detach_all(self):
        for name in list(self.attached):
            try:
                self.detach(name, quiesce_s=30)
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                print(f"cleanup detach {name}: {exc}", file=sys.stderr)


def store_env(fx: pathlib.Path, data: pathlib.Path = None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CCTALLY_", "CODEX_", "CLAUDE_"))}
    env.update({
        "CCTALLY_DATA_DIR": str(data or (fx / "data")),
        "CLAUDE_CONFIG_DIR": str(fx / "claude"),
        "HOME": str(fx / "home"),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "TZ": "Etc/UTC",
    })
    codex = sorted(str(p) for p in fx.glob("codex-*") if p.is_dir())
    if codex:
        env["CODEX_HOME"] = ",".join(codex)
    return env


# The probe runs in a child bound to the product tree. It reports the
# product's verdict and the raw identity facts every model needs.
PROBE = r'''
import ctypes, hashlib, importlib.machinery, importlib.util, json, os, pathlib, sys, uuid
bindir = sys.argv[1]
sys.path.insert(0, bindir)
loader = importlib.machinery.SourceFileLoader("cctally", os.path.join(bindir, "cctally"))
spec = importlib.util.spec_from_loader("cctally", loader)
mod = importlib.util.module_from_spec(spec)
sys.modules["cctally"] = mod
loader.exec_module(mod)
jr = mod._load_sibling("_cctally_journal")
lj = mod._load_sibling("_lib_journal")
core = sys.modules["_cctally_core"]
jd = pathlib.Path(core.JOURNAL_DIR)

libc = ctypes.CDLL(None, use_errno=True)
class AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32)]
libc.getattrlist.argtypes = [ctypes.c_char_p, ctypes.POINTER(AttrList),
                             ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint]
libc.getattrlist.restype = ctypes.c_int
def volume(path):
    al = AttrList(5, 0, 0, 0x80000000 | 0x00040000, 0, 0, 0)
    buf = ctypes.create_string_buffer(64)
    if libc.getattrlist(os.fsencode(str(path)), ctypes.byref(al), buf, 64, 0):
        return None
    if not 20 <= int.from_bytes(buf.raw[:4], sys.byteorder) <= 64:
        return None
    raw = buf.raw[4:20]
    return None if raw == bytes(16) else str(uuid.UUID(bytes=raw))
def st(p):
    try:
        s = os.stat(p)
    except OSError as exc:
        return {"error": type(exc).__name__}
    return {"dev": s.st_dev, "ino": s.st_ino, "size": s.st_size,
            "birth": getattr(s, "st_birthtime", None), "volume": volume(p)}

out = {"journalDir": str(jd), "valid": jr.quota_index_valid(jd)}
out["keys"] = st(jd / ".quota-observation-keys")
try:
    data = (jd / ".quota-observation-keys").read_bytes()
    out["keysSha256"] = hashlib.sha256(data).hexdigest()
    out["keysB64"] = __import__("base64").b64encode(data).decode("ascii")
    out["keysList"] = data.decode("ascii").split()
except OSError:
    out["keysSha256"], out["keysB64"], out["keysList"] = None, None, []
try:
    raw_meta = (jd / ".quota-observation-keys.meta").read_bytes()
    out["metaSha256"] = hashlib.sha256(raw_meta).hexdigest()
    out["meta"] = json.loads(raw_meta)
except (OSError, ValueError):
    out["meta"], out["metaSha256"] = None, None
out["segments"] = {n: st(jd / n) for n in jr.list_segments()}
mult, obs = {}, []
for name in jr.list_segments():
    with open(jd / name, "rb") as fh:
        for raw in fh:
            rec = lj.decode_line(raw)
            if rec is None:
                continue
            key = jr._codex_quota_natural_key(rec)
            if key is None:
                continue
            mult[key] = mult.get(key, 0) + 1
            p = rec.get("payload") or {}
            obs.append([key, p.get("source_path"), p.get("line_offset"),
                        p.get("logical_limit_key"), p.get("observed_slot")])
out["quotaObs"] = sum(mult.values())
out["multiplicity"] = mult
out["obs"] = obs
state = pathlib.Path(core.APP_DIR) / "quota-index-rebuild.json"
try:
    out["rebuildState"] = json.loads(state.read_text())
except (OSError, ValueError):
    out["rebuildState"] = None
print(json.dumps(out, sort_keys=True))
'''


def probe(tree: pathlib.Path, env):
    done = run([PY, "-I", "-B", "-c", PROBE, str(tree / "bin")], env=env)
    return json.loads(done.stdout.decode().strip().splitlines()[-1])


# The natural-key digest of each expected observation, computed by the tree's
# own journal content-id over the documented natural-key payload. Input: a
# JSON list of {source, source_path, line_offset, logical_limit_key}.
KEYGEN = r'''
import importlib.machinery, importlib.util, json, os, sys
bindir = sys.argv[1]
sys.path.insert(0, bindir)
loader = importlib.machinery.SourceFileLoader("cctally", os.path.join(bindir, "cctally"))
spec = importlib.util.spec_from_loader("cctally", loader)
mod = importlib.util.module_from_spec(spec)
sys.modules["cctally"] = mod
loader.exec_module(mod)
lj = mod._load_sibling("_lib_journal")
print(json.dumps([lj.content_id({"t": "quota-natural-key", "payload": p})
                  for p in json.loads(sys.stdin.read())]))
'''


def natural_keys(tree: pathlib.Path, env, payloads):
    done = run([PY, "-I", "-B", "-c", KEYGEN, str(tree / "bin")], env=env,
               stdin=json.dumps(payloads).encode())
    return json.loads(done.stdout.decode().strip().splitlines()[-1])


# ── identity models ─────────────────────────────────────────────────────────

def usable(birth):
    if isinstance(birth, bool):
        return False
    if isinstance(birth, int):
        return 0 < birth <= 2**53
    return isinstance(birth, float) and math.isfinite(birth) and birth > 0


RULES = {
    "devIno": lambda r, c: c["ino"] == r["ino"] and c["dev"] == r["dev"],
    "inodeOnly": lambda r, c: c["ino"] == r["ino"],
    "orRule": lambda r, c: c["ino"] == r["ino"] and (
        c["dev"] == r["dev"] or (usable(r["birth"]) and r["birth"] == c["birth"])),
    "strictBirth": lambda r, c: c["ino"] == r["ino"] and (
        r["birth"] == c["birth"] if usable(r["birth"]) and usable(c["birth"])
        else c["dev"] == r["dev"]),
}


def _volume_rule(r, c):
    if c["ino"] != r["ino"]:
        return False
    if usable(r["birth"]) and usable(c["birth"]) and r["birth"] != c["birth"]:
        return False
    if r["volume"] and c["volume"]:
        return r["volume"] == c["volume"]
    return c["dev"] == r["dev"]


RULES["volume"] = _volume_rule


def recorded_identity(p):
    """The identities the current sidecar records, completed with the birth
    and volume of the same files as observed right after publication."""
    meta = p["meta"]
    keys = dict(p["keys"], dev=meta["index_dev"], ino=meta["index_ino"])
    if p["keys"].get("ino") != meta["index_ino"]:
        raise RuntimeError("recorded_identity needs the published keys file")
    segs = {}
    for e in meta["coverage"]:
        s = p["segments"][e["segment"]]
        if s.get("ino") != e["ino"]:
            raise RuntimeError(f"recorded_identity: {e['segment']} replaced")
        segs[e["segment"]] = dict(s, dev=e["dev"], ino=e["ino"],
                                  extent=e["extent"])
    return {"keys": keys, "segments": segs, "indexBytes": meta["index_bytes"]}


def model_verdicts(rec, p):
    """Each rule's verdict with the C1 size rules applied, on probe ``p``."""
    out = {}
    keys = p["keys"]
    for name, rule in RULES.items():
        ok = "ino" in keys and keys["size"] >= rec["indexBytes"] and (
            keys["size"] - rec["indexBytes"]) % 19 == 0 and rule(rec["keys"], keys)
        for seg, r in rec["segments"].items():
            c = p["segments"].get(seg)
            ok = ok and bool(c) and "ino" in c and c["size"] >= r["extent"] \
                and rule(r, c)
        out[name] = bool(ok)
    return out


def same_except_device(a, b):
    return all(a.get(k) == b.get(k) for k in ("ino", "size", "birth", "volume")) \
        and a.get("dev") != b.get("dev")


def remount_preconditions(before, after):
    segs = [n for n in before["segments"]]
    return {
        "keysDeviceOnly": same_except_device(before["keys"], after["keys"]),
        "segmentsDeviceOnly": bool(segs) and all(
            same_except_device(before["segments"][n], after["segments"].get(n, {}))
            for n in segs),
        "keysBytesUnchanged": before["keysSha256"] == after["keysSha256"],
        "sidecarBytesUnchanged": (before["metaSha256"] is not None
                                  and before["metaSha256"] == after["metaSha256"]),
    }


# ── store construction ──────────────────────────────────────────────────────

def cctally(tree, env, *args, timeout=900):
    t0 = time.monotonic()
    done = run([PY, "-B", str(tree / "bin" / "cctally"), *args], env=env,
               timeout=timeout, check=False)
    return {"argv": list(args), "rc": done.returncode,
            "seconds": round(time.monotonic() - t0, 2),
            "stderrTail": done.stderr.decode(errors="replace").strip()[-400:]}


def append_fresh_quota_line(fx: pathlib.Path):
    """Append one NEW token_count event to a rollout: the newest rate_limits
    line's shape, one minute after the file's newest event, with a cumulative
    total above every earlier one (Codex ingest drops a non-increasing total
    as a duplicate before forming any quota observation) and the primary
    window half a point higher. Returns its path and byte offset, and the
    byte offset of the copied source line (whose journaled observations give
    the expected logical limit keys)."""
    best = None
    for path in sorted(fx.glob("codex-*/sessions/**/*.jsonl")):
        last, last_offset, newest, cumulative = None, None, None, 0
        offset = 0
        with open(path, "rb") as fh:
            for raw in fh:
                row = json.loads(raw)
                newest = max(newest or row["timestamp"], row["timestamp"])
                info = (row.get("payload") or {}).get("info") or {}
                total = (info.get("total_token_usage") or {}).get("total_tokens")
                if isinstance(total, int):
                    cumulative = max(cumulative, total)
                if info.get("rate_limits"):
                    last, last_offset = row, offset
                offset += len(raw)
        if last is not None:
            best = (path, last, last_offset, newest, cumulative)
    path, row, source_offset, newest, cumulative = best
    ts = dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
    row["timestamp"] = (ts + dt.timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z")
    row["payload"]["info"]["total_token_usage"] = {
        "total_tokens": cumulative + 1000}
    limits = row["payload"]["info"]["rate_limits"]
    limits["primary"]["used_percent"] = min(
        limits["primary"]["used_percent"] + 0.5, 100.0)
    offset = path.stat().st_size
    with open(path, "ab") as fh:
        fh.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
    return {"path": str(path), "offset": offset, "sourceOffset": source_offset}


def replace_same_bytes(path: pathlib.Path):
    tmp = path.with_name(path.name + ".q1080-replacement")
    shutil.copyfile(path, tmp)
    os.replace(tmp, path)


def forge_same_inode_store(src: pathlib.Path, dst: pathlib.Path, meta: dict,
                           births: "dict | None"):
    """On a fresh volume, create the keys file and every covered segment at
    the inode numbers and sizes ``meta`` records (create and unlink until
    the APFS object counter reaches each target). The keys content differs
    (every hex digit shifted), so a trusted index would be wrong. With
    ``births`` (name -> float) each file's birth time is forced to A's."""
    dst.mkdir(parents=True)
    targets = sorted([(meta["index_ino"], KEYS)]
                     + [(e["ino"], e["segment"]) for e in meta["coverage"]])
    attempts = 0
    shift = str.maketrans("0123456789abcdef", "123456789abcdef0")
    for ino, name in targets:
        data = (src / name).read_bytes()
        if name == KEYS:
            data = "\n".join(l[:2] + l[2:].translate(shift) if l else l
                             for l in data.decode("ascii").split("\n")
                             ).encode("ascii")
        while True:
            attempts += 1
            if attempts > 500_000:
                raise RuntimeError("inode forging exceeded its attempt bound")
            tmp = dst / ".forge"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            got = os.fstat(fd).st_ino
            if got != ino:
                os.close(fd)
                os.unlink(tmp)
                if got > ino:
                    raise RuntimeError(f"overshot inode {ino} for {name}: {got}")
                continue
            os.write(fd, data)
            os.close(fd)
            os.replace(tmp, dst / name)
            break
        if births is not None:
            b = births[name]
            sec = math.floor(b)
            ns = sec * 10**9 + round((b - sec) * 1e9)
            os.utime(dst / name, ns=(ns, ns))
    shutil.copyfile(src / META, dst / META)
    return {"targets": len(targets), "attempts": attempts}


def forged_preconditions(rec, p, *, births: bool):
    k = p["keys"]
    out = {
        "keysInoSize": (k.get("ino"), k.get("size")) == (
            rec["keys"]["ino"], rec["keys"]["size"]),
        "segmentsInoSize": all(
            (p["segments"].get(n, {}).get("ino"), p["segments"].get(n, {}).get("size"))
            == (r["ino"], r["size"]) for n, r in rec["segments"].items()),
        "volumeDiffers": k.get("volume") != rec["keys"]["volume"],
    }
    if births:
        out["birthsEqual"] = k.get("birth") == rec["keys"]["birth"] and all(
            p["segments"].get(n, {}).get("birth") == r["birth"]
            for n, r in rec["segments"].items())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=pathlib.Path)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--work", required=True, type=pathlib.Path)
    ap.add_argument("--label", required=True)
    args = ap.parse_args()
    if sys.platform != "darwin":
        print("quota-index-remount: macOS only (hdiutil, getattrlist)",
              file=sys.stderr)
        sys.exit(2)
    args.work.mkdir(parents=True, exist_ok=False)
    commit = run(["git", "-C", str(args.repo), "rev-parse", "--verify",
                  f"{args.sha}^{{commit}}"]).stdout.decode().strip()
    tree = args.work / "tree"
    tree.mkdir()
    archive = run(["git", "-C", str(args.repo), "archive", commit]).stdout
    run(["tar", "-x", "-C", str(tree)], stdin=archive)
    receipt = {"commit": commit, "treeSha": run(
        ["git", "-C", str(args.repo), "rev-parse", f"{commit}^{{tree}}"]
    ).stdout.decode().strip(), "python": sys.version.split()[0],
        "startedAt": dt.datetime.now(dt.timezone.utc).isoformat()}
    checks, models, commands = {}, {}, []
    # Set true only as the last step: an aborted run must fail the verdict.
    checks["completed"] = False
    images = Images(args.work, args.label)

    def sync(env, label):
        result = cctally(tree, env, "cache-sync", "--source", "all")
        result["label"] = label
        commands.append(result)
        checks[f"rc0:{label}"] = result["rc"] == 0
        return result

    try:
        for name in ("A", "S", "B", "C", "D"):
            images.create(name)
        a = images.attach("A")
        fx = a / "fx"
        built = run([PY, "-B", str(tree / "bin" / "build-bench-fixtures.py"),
                     "--scale", "small", "--seed", "7", "--out", str(fx)],
                    env=store_env(fx), timeout=1800, check=False)
        checks["rc0:fixture"] = built.returncode == 0
        # The fixture creates the codex-* roots, so the environment is built
        # only now; before, CODEX_HOME would be unset and no sync would walk
        # a single Codex file.
        env = store_env(fx)
        checks["codexRootsConfigured"] = bool(env.get("CODEX_HOME"))
        sync(env, "sync1")
        p1 = probe(tree, env)
        checks["sync1Valid"] = p1["valid"]
        checks["sync1HasQuotaObservations"] = p1["quotaObs"] > 0
        rec1 = recorded_identity(p1)

        # R1: device-only remount (the spacer takes A's number).
        images.detach("A")
        images.attach("S")
        if images.attach("A") != a:
            raise RuntimeError("A remounted elsewhere")
        p2 = probe(tree, env)
        for k, v in remount_preconditions(p1, p2).items():
            checks[f"remount1:{k}"] = v
        models["remount1"] = dict(model_verdicts(rec1, p2), product=p2["valid"])
        checks["remount1:validBeforeSync"] = p2["valid"]

        fresh = append_fresh_quota_line(fx)
        receipt["freshLine"] = fresh
        # The oracle, fixed before sync-2: the copied source line's journaled
        # observations (same rollout, root, limit id, slot, window, model)
        # give each slot's retained logical_limit_key; the injected line's
        # keys are those at the injected line's own path and offset.
        source_obs = sorted({
            (o[4], o[3]) for o in p2["obs"]
            if o[1] == fresh["path"] and o[2] == fresh["sourceOffset"]})
        expected = natural_keys(tree, env, [
            {"source": "codex", "source_path": fresh["path"],
             "line_offset": fresh["offset"], "logical_limit_key": limit_key}
            for _slot, limit_key in source_obs])
        receipt["expectedKeys"] = expected
        checks["sync2:expectedDerivable"] = (
            len(source_obs) == 2 and len(set(o[0] for o in source_obs)) == 2
            and len(set(expected)) == 2
            and not (set(expected) & set(p2["keysList"])))
        sync(env, "sync2")
        p3 = probe(tree, env)
        want = dict(p2["multiplicity"])
        for k in expected:
            want[k] = want.get(k, 0) + 1
        checks["sync2:journalGainedExpectedOnce"] = p3["multiplicity"] == want
        before = base64.b64decode(p2["keysB64"] or "")
        after = base64.b64decode(p3["keysB64"] or "")
        suffix = after[len(before):]
        checks["sync2:keysSameFile"] = all(
            p3["keys"].get(k) == p2["keys"].get(k)
            for k in ("ino", "birth", "volume"))
        checks["sync2:keysBytesPrefixKept"] = after[:len(before)] == before
        checks["sync2:keysGrewByExpectedLines"] = (
            len(suffix) == 38
            and sorted(suffix.decode("ascii", "replace").split("\n")[:-1]) == sorted(expected)
            and suffix.endswith(b"\n"))
        checks["sync2:rebuildStateUntouched"] = p3["rebuildState"] == p2["rebuildState"]
        checks["sync2:valid"] = p3["valid"]

        # R2: remount back to A's first number.
        images.detach("A")
        images.detach("S")
        images.attach("A")
        p4 = probe(tree, env)
        for k, v in remount_preconditions(p3, p4).items():
            checks[f"remount2:{k}"] = v
        rec3 = recorded_identity(p3)
        models["remount2"] = dict(model_verdicts(rec3, p4), product=p4["valid"])
        checks["remount2:validBeforeSync"] = p4["valid"]
        sync(env, "sync3")
        p5 = probe(tree, env)
        checks["sync3:keysUnchanged"] = (
            p5["keysSha256"] == p4["keysSha256"]
            and all(p5["keys"].get(k) == p4["keys"].get(k)
                    for k in ("ino", "birth", "volume")))
        checks["sync3:multiplicityUnchanged"] = p5["multiplicity"] == p4["multiplicity"]
        checks["sync3:rebuildStateUntouched"] = p5["rebuildState"] == p4["rebuildState"]
        checks["sync3:valid"] = p5["valid"]
        checks["sync3:indexCoversJournal"] = set(p5["multiplicity"]) <= set(
            p5["keysList"])
        rec5 = recorded_identity(p5)

        # N: controls that leave A untouched.
        controls = {}
        journal = fx / "data" / "journal"
        run(["cp", "-Rp", str(fx / "data"), str(fx / "data-copy")])
        controls["copiedStore"] = probe(tree, store_env(fx, fx / "data-copy"))
        b = images.attach("B")
        run(["cp", "-Rp", str(fx / "data"), str(b / "data")])
        controls["differentVolume"] = probe(tree, store_env(fx, b / "data"))
        births = {KEYS: rec5["keys"]["birth"],
                  **{n: r["birth"] for n, r in rec5["segments"].items()}}
        forged = {}
        for name, with_births in (("C", False), ("D", True)):
            mount = images.attach(name)
            forged[name] = forge_same_inode_store(
                journal, mount / "data" / "journal", p5["meta"],
                births if with_births else None)
        controls["sameInodeCrossVolume"] = probe(
            tree, store_env(fx, images.attached["C"][1] / "data"))
        controls["sameInodeSameBirthCrossVolume"] = probe(
            tree, store_env(fx, images.attached["D"][1] / "data"))
        pre = {
            "sameInodeCrossVolume": forged_preconditions(
                rec5, controls["sameInodeCrossVolume"], births=False),
            "sameInodeSameBirthCrossVolume": forged_preconditions(
                rec5, controls["sameInodeSameBirthCrossVolume"], births=True),
        }
        for name in pre:
            pre[name]["deviceDiffers"] = (
                controls[name]["keys"]["dev"] != rec5["keys"]["dev"])

        # N: mutating controls on A.
        replace_same_bytes(journal / KEYS)
        controls["replacedKeys"] = probe(tree, env)
        sync(env, "restore")
        restored = probe(tree, env)
        checks["restoredValid"] = restored["valid"]
        rec_restored = recorded_identity(restored)
        newest = max(rec_restored["segments"],
                     key=lambda n: (not n.startswith("bootstrap-"), n))
        replace_same_bytes(journal / newest)
        controls["replacedSegment"] = probe(tree, env)

        # N: device reuse. Detach everything, attach each forged image alone.
        images.detach_all()
        for name, label in (("C", "deviceReuseCrossVolume"),
                            ("D", "deviceReuseSameBirthCrossVolume")):
            mount = images.attach(name)
            controls[label] = probe(tree, store_env(fx, mount / "data"))
            pre[label] = forged_preconditions(rec5, controls[label],
                                              births=(name == "D"))
            pre[label]["deviceReused"] = (
                controls[label]["keys"]["dev"] == rec5["keys"]["dev"])
            images.detach(name)

        recorded_for = {"replacedKeys": rec5, "replacedSegment": rec_restored}
        for name, p in controls.items():
            rec = recorded_for.get(name, rec5)
            models[name] = dict(model_verdicts(rec, p), product=p["valid"])
            checks[f"control:{name}:invalid"] = not p["valid"]
        for name, conds in pre.items():
            checks[f"control:{name}:precondition"] = all(conds.values())
        receipt["preconditions"] = pre
        receipt["forged"] = forged
        checks["completed"] = True
    finally:
        images.detach_all()
        receipt["finishedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        receipt["checks"] = checks
        receipt["models"] = models
        receipt["commands"] = commands
        receipt["allChecks"] = bool(checks) and all(checks.values())
        print(json.dumps(receipt, sort_keys=True, indent=1, default=str))


if __name__ == "__main__":
    main()
