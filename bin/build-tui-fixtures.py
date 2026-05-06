#!/usr/bin/env python3
"""Regenerate TUI golden frames.

For each (state, variant, size) combo:
  1. Invoke `cctally tui --render-once
     --snapshot-module <path> --variant <v> --force-size WxH --no-color`
  2. Capture stdout
  3. Write to tests/fixtures/tui/golden/<state>_<variant>_<WxH>.txt

Eyeball each .txt after generation: misaligned borders, tag-literal
leakage, or crash output means fix the renderer (not the golden).
"""
import os
import pathlib
import subprocess

REPO = pathlib.Path(__file__).parent.parent
FIX = REPO / "tests" / "fixtures" / "tui"
GOLDEN = FIX / "golden"
GOLDEN.mkdir(parents=True, exist_ok=True)
BIN = REPO / "bin" / "cctally"

COMBOS = [
    ("warn", "conventional", "120x36"),
    ("warn", "conventional", "100x30"),
    ("warn", "expressive",   "120x36"),
    ("warn", "expressive",   "100x30"),
    ("ok",   "conventional", "120x36"),
    ("over", "conventional", "120x36"),
]

for state, variant, size in COMBOS:
    mod = FIX / f"snapshot_{state}.py"
    assert mod.exists(), f"missing {mod}"
    out_file = GOLDEN / f"{state}_{variant}_{size}.txt"
    result = subprocess.run(
        [
            str(BIN), "tui", "--render-once",
            "--snapshot-module", str(mod),
            "--variant", variant,
            "--force-size", size,
            "--no-color",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "NO_COLOR": "1", "TZ": "UTC"},
    )
    out_file.write_text(result.stdout)
    print(f"wrote {out_file.relative_to(REPO)}")
