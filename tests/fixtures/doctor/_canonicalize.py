#!/usr/bin/env python3
"""Canonicalize cctally-doctor output for byte-stable goldens.

Reads stdin, writes stdout. argv[1] is the scratch HOME prefix to
redact to literal `$HOME`. Also redacts:
- `cctally_version` (top-level JSON field, reads CHANGELOG.md so it
  bumps every release).
"""
import hashlib
import pathlib
import re
import sys

scratch = sys.argv[1]
data = sys.stdin.read()
data = data.replace(scratch, "$HOME")
# Root keys intentionally hash canonical paths.  Doctor fixtures create two
# fake Codex homes under the randomized scratch directory, so map their raw
# key order to stable opaque labels after the doctor has sorted by full key.
keys = []
for suffix in (".codex", "codex-a", "codex-b"):
    path = pathlib.Path(scratch, suffix)
    if path.exists():
        digest = hashlib.sha256(
            b"cctally-source-root-v1\0" + str(path.resolve()).encode("utf-8")
        ).hexdigest()[:32]
        keys.append(digest)
for index, key in enumerate(sorted(keys)):
    data = data.replace(key, f"$CODEX_ROOT_{index + 1}")
data = re.sub(
    r'("cctally_version"\s*:\s*)"[^"]+"',
    r'\1"<redacted>"',
    data,
)
# #496 S6: `db.retained_artifacts` reports the host's real free disk, which
# changes between two runs on the same machine and differs across the two LAN
# runners. Redact it for the same reason `cctally_version` is redacted — the
# value is genuinely useful in the report and genuinely unstable in a golden.
# DIGITS ONLY. Redacting `null`/`None` too would absorb the loss of the
# measurement itself: a regression that stopped measuring free disk would
# render `null`, get redacted to the same `<redacted>` token, and move no
# golden at all.
data = re.sub(
    r'("freeDiskBytes"\s*:\s*)\d+',
    r'\1"<redacted>"',
    data,
)
# The verbose text dump prints the same key UNQUOTED, so a JSON-only pattern
# left half the divergence in place — which is how the first regeneration
# still produced 12 failing scenarios.
data = re.sub(
    r'(?<!")(\bfreeDiskBytes\s*:\s*)\d+',
    r'\1<redacted>',
    data,
)
sys.stdout.write(data)
