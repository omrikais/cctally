"""Transcript-root growth for the #901 runners (spec §6.3; revision 15, Q16:
per-file growth instead of aggregate totals, which can hide a deletion or a
replacement behind growth elsewhere).

usage: jsonl_bytes.py [--baseline FILE] [--frontier FILE]
  Walks the roots the tested tree reads ($CLAUDE_CONFIG_DIR, else ~/.claude,
  -> projects/**/*.jsonl; $CODEX_HOME, else ~/.codex, -> sessions/**/*.jsonl)
  and prints one JSON line: {"t", "claude": total bytes, "codex": total bytes}
  (the aggregate the existing readers use) and, with --baseline, per-file
  evidence against FILE (written on the first call, read on every later one):
  "growth" {claude, codex} (bytes appended to baseline files plus new files),
  "discontinuities" (shrunk, replaced or deleted files) and "changes"
  {path: {provider, kind: grown|new|shrunk|replaced|deleted, base, size}}.
  In a frozen run it is started through the namespace, so it measures the
  freeze, whose growth must be zero. --frontier FILE (Amendment 19 HR-17,
  `run-live.sh`) also writes this snapshot as a live finite-frontier receipt:
  {"mode": "live", "capturedAt", "files": {path: {provider, size, dev, ino}}}.
"""
import json
import os
import sys
import time


def _roots(env_name, default, sub):
    raw = os.environ.get(env_name)
    bases = [p for p in raw.split(",") if p] if raw else [default]
    return [os.path.join(os.path.expanduser(b), sub) for b in bases]


def snapshot():
    files = {}
    for provider, roots in (
            ("claude", _roots("CLAUDE_CONFIG_DIR", "~/.claude", "projects")),
            ("codex", _roots("CODEX_HOME", "~/.codex", "sessions"))):
        for root in roots:
            for dirpath, _dirs, names in os.walk(root):
                for name in names:
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    files[path] = [provider, st.st_size, st.st_dev, st.st_ino]
    return files


def compare(base, now):
    changes, growth, broken = {}, {"claude": 0, "codex": 0}, 0
    for path, (provider, size, dev, ino) in now.items():
        old = base.get(path)
        if old is None:
            changes[path] = {"provider": provider, "kind": "new", "base": 0,
                             "size": size}
            growth[provider] += size
        elif (old[2], old[3]) != (dev, ino):
            changes[path] = {"provider": provider, "kind": "replaced",
                             "base": old[1], "size": size}
            broken += 1
        elif size < old[1]:
            changes[path] = {"provider": provider, "kind": "shrunk",
                             "base": old[1], "size": size}
            broken += 1
        elif size > old[1]:
            changes[path] = {"provider": provider, "kind": "grown",
                             "base": old[1], "size": size}
            growth[provider] += size - old[1]
    for path, (provider, size, _dev, _ino) in base.items():
        if path not in now:
            changes[path] = {"provider": provider, "kind": "deleted",
                             "base": size, "size": 0}
            broken += 1
    return changes, growth, broken


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    baseline = argv[argv.index("--baseline") + 1] if "--baseline" in argv else None
    now = snapshot()
    out = {"t": time.time(),
           "claude": sum(v[1] for v in now.values() if v[0] == "claude"),
           "codex": sum(v[1] for v in now.values() if v[0] == "codex")}
    if baseline:
        if os.path.exists(baseline):
            with open(baseline) as fh:
                base = json.load(fh)
        else:
            base = now
            with open(baseline, "w") as fh:
                json.dump(now, fh)
        changes, growth, broken = compare(base, now)
        out.update(growth=growth, discontinuities=broken, changes=changes,
                   files=len(now))
    if "--frontier" in argv:
        frontier = argv[argv.index("--frontier") + 1]
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(out["t"]))
        with open(frontier, "w") as fh:
            json.dump({"mode": "live", "capturedAt": stamp,
                       "source": "jsonl_bytes.py",
                       "files": {path: {"provider": v[0], "size": v[1],
                                        "dev": v[2], "ino": v[3]}
                                 for path, v in sorted(now.items())}}, fh)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
