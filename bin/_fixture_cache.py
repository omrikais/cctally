#!/usr/bin/env python3
"""Content-hash-keyed cache for golden-harness SQLite fixtures (#281 S11 / R9).

Memoizes the deterministic fixture trees bin/build-*-fixtures.py builders write
into a --out scratch dir. Safety contract: a cache problem NEVER changes a test
outcome; every failure mode falls back to running the builder. Only
CCTALLY_FIXTURE_CACHE_VERIFY=1 fails loud. See the S11 design record.

CLI:  _fixture_cache.py run --builder <build-X-fixtures.py> --out <scratch_dir>
Env:  CCTALLY_FIXTURE_CACHE=0        bypass (run builder, no cache)
      CCTALLY_FIXTURE_CACHE_DIR=...  cache location override
      CCTALLY_FIXTURE_CACHE_VERIFY=1 audit (loud on corruption / stale content)
"""
from __future__ import annotations
import argparse, ast, hashlib, os, shutil, sqlite3, subprocess, sys, tempfile
from pathlib import Path

CACHE_FORMAT_VERSION = 2
BIN_DIR = Path(__file__).resolve().parent
_TRANSIENT = (".db-wal", ".db-shm", ".db-journal",
              ".sqlite-wal", ".sqlite-shm", ".sqlite-journal")
# Curated allowlist — the ONLY env vars passed to a builder subprocess, so an
# ambient var can never change fixture bytes without changing the key. Expand
# only if a wired builder errors under the sanitized env (never add a byte-
# affecting var like CCTALLY_MIGRATION_TEST_MODE).
_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "TZ",
             "LANG", "LC_ALL", "LC_CTYPE", "CCTALLY_DISABLE_DEV_AUTODETECT")


def _emit(marker: str, label: str) -> None:
    sys.stderr.write(f"FIXTURE-CACHE {marker} {label}\n"); sys.stderr.flush()


def label_for(builder_path: Path) -> str:
    n = Path(builder_path).name
    if n.startswith("build-") and n.endswith("-fixtures.py"):
        return n[len("build-"):-len("-fixtures.py")]
    return n


def _module_file(mod: str) -> "Path | None":
    cand = BIN_DIR / (mod.split(".")[0] + ".py")
    return cand if cand.is_file() else None


def transitive_bin_sources(builder_path: Path) -> "list[Path]":
    seen: "set[Path]" = set(); order: "list[Path]" = []
    def visit(p: Path) -> None:
        p = Path(p).resolve()
        if p in seen or not p.is_file(): return
        seen.add(p); order.append(p)
        try: tree = ast.parse(p.read_bytes(), filename=str(p))
        except SyntaxError: return
        names: "set[str]" = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names: names.add(a.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module)
        for mod in sorted(names):
            f = _module_file(mod)
            if f is not None: visit(f)
    visit(Path(builder_path))
    return sorted(order)


def sqlite_fingerprint() -> "tuple[str, tuple[str, ...], bool]":
    conn = sqlite3.connect(":memory:")
    try:
        opts = tuple(sorted(r[0] for r in conn.execute("PRAGMA compile_options")))
        try:
            conn.execute("CREATE VIRTUAL TABLE _p USING fts5(x)"); fts5 = True
        except sqlite3.Error: fts5 = False
    finally: conn.close()
    return sqlite3.sqlite_version, opts, fts5


def python_identity() -> str:
    return f"{sys.implementation.cache_tag}|{tuple(sys.version_info[:3])}"


def _h(h, data: bytes) -> None:
    h.update(len(data).to_bytes(8, "big")); h.update(data)


def compute_key(builder_path, *, sqlite_version, compile_options,
                fts5_available, python_id) -> str:
    d = hashlib.sha256()
    _h(d, str(CACHE_FORMAT_VERSION).encode())
    for src in transitive_bin_sources(builder_path):
        _h(d, src.name.encode()); _h(d, src.read_bytes())
    _h(d, sqlite_version.encode())
    for opt in compile_options: _h(d, opt.encode())
    _h(d, b"fts5:1" if fts5_available else b"fts5:0")
    _h(d, python_id.encode())
    return d.hexdigest()


def cache_root() -> Path:
    ov = os.environ.get("CCTALLY_FIXTURE_CACHE_DIR")
    if ov: return Path(ov)
    xdg = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(xdg) / "cctally" / "fixture-cache"


def _cacheable(p: Path) -> bool:
    return p.is_file() and not p.is_symlink() and not p.name.endswith(_TRANSIENT)


def build_manifest(root: Path) -> str:
    lines = []
    for p in sorted(root.rglob("*")):
        if p.name == "MANIFEST" or not _cacheable(p): continue
        rel = p.relative_to(root).as_posix()
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {rel}")
    return "\n".join(lines) + "\n"


def validate_tree(root: Path, manifest_text: str) -> bool:
    return build_manifest(root) == manifest_text


def _sanitized_env() -> dict:
    return {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}


def _run_builder(builder: Path, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    return subprocess.run([sys.executable, str(builder), "--out", str(out)],
                          env=_sanitized_env(), stdout=subprocess.DEVNULL).returncode


def _copy_tree_into(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name == "MANIFEST": continue
        if item.is_dir():
            shutil.copytree(item, dest / item.name, symlinks=True,
                            dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*(f"*{s}" for s in _TRANSIENT)))
        elif _cacheable(item):
            shutil.copy2(item, dest / item.name)


def _atomic_publish(src_out: Path, entry: Path) -> None:
    root = entry.parent; root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{entry.name}-", dir=root))
    try:
        _copy_tree_into(src_out, tmp)
        (tmp / "MANIFEST").write_text(build_manifest(tmp))
        try:
            os.rename(tmp, entry)          # atomic; fails if entry already exists
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)   # another writer won; fine
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True); raise


def _clear_dir(d: Path) -> None:
    if not d.exists(): return
    for item in d.iterdir():
        if item.is_dir(): shutil.rmtree(item, ignore_errors=True)
        else:
            try: item.unlink()
            except OSError: pass


def _restore(entry: Path, out: Path) -> bool:
    _copy_tree_into(entry, out)
    return validate_tree(out, (entry / "MANIFEST").read_text())


def _try_store(out: Path, entry: Path, lock: Path) -> None:
    import fcntl
    lock.parent.mkdir(parents=True, exist_ok=True)
    try: fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError: return
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: return          # another process publishing; build-without-store
        if entry.is_dir():
            try:
                if validate_tree(entry, (entry / "MANIFEST").read_text()): return
            except Exception: shutil.rmtree(entry, ignore_errors=True)
        try: _atomic_publish(out, entry)
        except Exception: pass          # store is best-effort
    finally:
        try: fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError: pass
        os.close(fd)


def _tree_diff(a: Path, b: Path) -> "list[str]":
    def inv(root: Path) -> dict:
        d = {}
        for p in sorted(root.rglob("*")):
            if p.name.endswith(_TRANSIENT): continue
            rel = p.relative_to(root).as_posix()
            if p.is_symlink(): d[rel] = ("link", os.readlink(p))
            elif p.is_dir(): d[rel] = ("dir", None)
            else: d[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())
        return d
    ia, ib = inv(a), inv(b); diffs = []
    for rel in sorted(set(ia) | set(ib)):
        if ia.get(rel) != ib.get(rel):
            diffs.append(f"  {rel}: cached={ia.get(rel)} fresh={ib.get(rel)}")
    return diffs


def _verify_clean_hit(builder: Path, out: Path, label: str) -> int:
    with tempfile.TemporaryDirectory(prefix=f"fcverify-{label}-") as td:
        fresh = Path(td) / "fresh"
        rc = _run_builder(builder, fresh)
        if rc != 0:
            sys.stderr.write(f"fixture-cache: VERIFY build failed {label}\n"); return rc
        diffs = _tree_diff(out, fresh)
        if diffs:
            sys.stderr.write(
                f"fixture-cache: AUDIT FAILURE {label} — cached tree differs from a "
                f"fresh build (key incomplete or non-relocatable):\n"
                + "\n".join(diffs[:20]) + "\n")
            return 3
    return 0


def run(builder: Path, out: Path) -> int:
    builder = Path(builder); out = Path(out)
    label = label_for(builder)
    verify = os.environ.get("CCTALLY_FIXTURE_CACHE_VERIFY") == "1"
    if os.environ.get("CCTALLY_FIXTURE_CACHE") == "0":
        _emit("BYPASS", label); return _run_builder(builder, out)
    try:
        sv, opts, fts5 = sqlite_fingerprint()
        key = compute_key(builder, sqlite_version=sv, compile_options=opts,
                          fts5_available=fts5, python_id=python_identity())
        entry = cache_root() / f"{label}__{key}"
        lock = cache_root() / f"{label}__{key}.lock"
    except Exception:
        _emit("MISS", label); return _run_builder(builder, out)
    if entry.is_dir():
        corrupt = False
        try:
            # _restore copies the entry into <out> then re-validates <out>
            # against the entry's MANIFEST. A poisoned entry copies to a
            # poisoned <out> → manifest mismatch → False, so this single
            # post-copy hash is sufficient; no redundant pre-copy hash of the
            # entry. A missing/corrupt MANIFEST raises → caught below → corrupt.
            if _restore(entry, out):
                _emit("HIT", label)
                return _verify_clean_hit(builder, out, label) if verify else 0
            corrupt = True
        except Exception:
            corrupt = True
        if corrupt:
            _emit("POISONED", label)
            if verify:
                sys.stderr.write(f"fixture-cache: AUDIT FAILURE (corrupt entry) {label}\n")
                return 3
            shutil.rmtree(entry, ignore_errors=True); _clear_dir(out)
    rc = _run_builder(builder, out)
    if rc != 0: _emit("MISS", label); return rc
    _try_store(out, entry, lock); _emit("MISS", label); return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="_fixture_cache.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--builder", required=True, type=Path)
    r.add_argument("--out", required=True, type=Path)
    ns = ap.parse_args(argv)
    if ns.cmd == "run": return run(ns.builder, ns.out)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
