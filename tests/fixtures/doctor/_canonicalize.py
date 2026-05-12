#!/usr/bin/env python3
"""Canonicalize cctally-doctor output for byte-stable goldens.

Reads stdin, writes stdout. argv[1] is the scratch HOME prefix to
redact to literal `$HOME`. Also redacts:
- `cctally_version` (top-level JSON field, reads CHANGELOG.md so it
  bumps every release).
"""
import re
import sys

scratch = sys.argv[1]
data = sys.stdin.read()
data = data.replace(scratch, "$HOME")
data = re.sub(
    r'("cctally_version"\s*:\s*)"[^"]+"',
    r'\1"<redacted>"',
    data,
)
sys.stdout.write(data)
