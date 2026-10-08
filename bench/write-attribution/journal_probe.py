"""#901 spec §6.3 "Statement-journal evidence" (Q11): the maximum journal
extent per statement execution, per affected writer family.

usage: journal_probe.py SQLATTR_JSON [SQLATTR_JSON ...] --out OUT.json [--input-bytes N]

The measurement is a separately LABELLED FILE-control on an equivalent clone:
run the tested tree's sync or ingest under `sqlattr.py` with
`SQLATTR_TEMP_STORE=DEFAULT` (the enumerated writers keep SQLite's default
file temp store) and the interposer, for example

    SQLATTR_TEMP_STORE=DEFAULT SQLATTR_OUT=$OUT/jp.sqlattr.json \\
      SQLATTR_TREE=$TREE SQLATTR_DYLIB=$DYLIB WTRACE_OUT=$OUT/jp \\
      DYLD_INSERT_LIBRARIES=$DYLIB python3 sqlattr.py cache-sync --source all

Each statement's largest single temp charge is the high-water of its
`etilqs_*` statement journal for one execution (a statement journal is
written sequentially once). This tool groups the statements into the writer
families §1.4 attributed and reports, per family, the maximum extent with its
SQL and call site, its calls, its cumulative temp bytes (kept distinct from
the maximum) and the run's runtime and input size. FILE controls are
diagnostic evidence, never a substitute for the MEMORY-mode A/B/D runs; an
unlabelled (MEMORY) input is refused, because zero disk temp bytes there say
nothing about the in-memory journal.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys

#: §1.4's statement-journal families (Q11), matched on the normalized SQL.
FAMILIES = (
    ("fts_segment_write", re.compile(r"REPLACE INTO .*_(data|idx)\b", re.I)),
    ("fts_segment_write", re.compile(r"REPLACE INTO .*VALUES\s*\(\?,\s*\?\)", re.I)),
    ("quota_snapshot_insert",
     re.compile(r"INSERT OR IGNORE INTO quota_window_snapshots", re.I)),
    ("quota_snapshot_delete", re.compile(r"DELETE FROM quota_window_snapshots", re.I)),
    ("codex_file_cursor",
     re.compile(r"INSERT OR REPLACE INTO codex_session_files", re.I)),
    ("claude_entries", re.compile(r"INTO session_entries", re.I)),
    ("codex_entries", re.compile(r"INTO codex_session_entries", re.I)),
    ("claude_transcript", re.compile(r"INTO conversation_", re.I)),
    ("codex_transcript", re.compile(r"INTO codex_conversation_", re.I)),
    ("stats_ingest", re.compile(r"INTO (weekly_|percent_|five_hour|quota_)", re.I)),
)


def family(sql: str) -> str:
    for name, pattern in FAMILIES:
        if pattern.search(sql):
            return name
    return "other"


def summarize(runs: "list[dict]", *, input_bytes=None) -> dict:
    """Per-family maxima over one or more FILE-control sqlattr outputs."""
    for run in runs:
        if not (run.get("control") or {}).get("tempStore"):
            raise ValueError("not a labelled FILE-control run (set "
                             "SQLATTR_TEMP_STORE=DEFAULT)")
    families = {}
    for run in runs:
        for key, calls, temp, _other, max_temp, _wall in run["stmts"]:
            sql, _sep, site = key.partition("  @@  ")
            entry = families.setdefault(family(sql), {
                "maxJournalBytes": 0, "statement": None, "callSite": None,
                "calls": 0, "cumulativeTempBytes": 0})
            entry["calls"] += calls
            entry["cumulativeTempBytes"] += temp
            if max_temp > entry["maxJournalBytes"]:
                entry.update(maxJournalBytes=max_temp, statement=sql,
                             callSite=site)
    return {"method": "FILE-control via sqlattr.py (SQLATTR_TEMP_STORE="
                      + runs[0]["control"]["tempStore"] + ")",
            "runtime": {"python": sys.version.split()[0],
                        "sqlite": sqlite3.sqlite_version},
            "inputBytes": input_bytes,
            "unattributedTempBytes": [(r.get("unattributed") or {}).get("temp")
                                      for r in runs],
            "footprintPeakBytes": [r.get("footprintPeakBytes") for r in runs],
            "families": dict(sorted(families.items()))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--out", required=True)
    parser.add_argument("--input-bytes", type=int)
    args = parser.parse_args(argv)
    runs = [json.load(open(path)) for path in args.inputs]
    try:
        report = summarize(runs, input_bytes=args.input_bytes)
    except ValueError as exc:
        print(f"INVALID: {exc}")
        return 2
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(json.dumps({k: v["maxJournalBytes"]
                      for k, v in report["families"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
