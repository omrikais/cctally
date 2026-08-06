"""#496 S4 — shared production-shaped journal fixture driver.

Every measured or compared rebuild in this session runs in a FRESH subprocess
over its OWN cloned data dir (spec §3, §8.5). This module is the one place that
knows how to spawn one and how to reduce its dump to the durable comparison
view, so the tests state what they assert rather than how to spawn a process.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

_TESTS = pathlib.Path(__file__).resolve().parent
_REPO = _TESTS.parent
_WORKER = _TESTS / "_rebuild_worker_496_s4.py"

#: Tier 1 runs inside `bin/cctally-test-all`'s 120s per-test timeout, so it is
#: sized in tens of thousands of lines rather than a million (spec §8.2).
TIER1_LINES = 12_000

GOLDEN_DIR = _TESTS / "fixtures" / "rebuild_read_path"

#: Durable outputs the canonical dumps pin. Timings, resident peaks and
#: traversal counters are deliberately NOT here — they are asserted separately
#: and would make the goldens machine-dependent.
CANONICAL_KEYS = (
    "stats",
    "journal_effective_events",
    "journal_protocol_violations",
    "accounts_last_seen",
    "cache",
    "rows_by_table",
    "lines_folded",
    "malformed",
    "segments_read",
    "conflicts",
    "protocol_violations",
    "acknowledged_protocol_violations",
)


def run_worker(
    work_dir,
    name: str,
    *,
    build: "dict | None" = None,
    pin: "str | None" = None,
    trace: bool = False,
    no_quota_cache: bool = False,
    timeout: float = 600.0,
) -> dict:
    """One rebuild in a fresh subprocess over a fresh data dir; its dump."""
    root = pathlib.Path(work_dir) / name
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.json"
    out_path = root / "dump.json"
    config = {
        "data_dir": str(root / "share"),
        "home": str(root / "home"),
        "out": str(out_path),
        "build": build,
        "pin": pin,
        "tracemalloc": trace,
        "no_quota_cache": no_quota_cache,
    }
    config_path.write_text(json.dumps(config))

    env = dict(os.environ)
    env["HOME"] = str(root / "home")
    env["CCTALLY_DATA_DIR"] = str(root / "share")
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    env["CCTALLY_DISABLE_TELEMETRY"] = "1"
    env.setdefault("TZ", "Etc/UTC")
    env.pop("CLAUDE_CONFIG_DIR", None)

    completed = subprocess.run(
        [sys.executable, str(_WORKER), str(config_path)],
        cwd=str(_REPO), env=env, capture_output=True, text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"rebuild worker {name} failed (exit {completed.returncode})\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return json.loads(out_path.read_text())


#: Columns whose value is a per-rebuild nonce rather than derived data.
#: `quota_percent_milestones.generation` is minted once per re-materialization,
#: so two identical rebuilds legitimately disagree on it. It is CANONICALIZED
#: rather than dropped — each distinct value becomes a stable ordinal — so the
#: grouping it expresses is still pinned by the goldens.
NONCE_COLUMNS = ("generation",)


def _canonicalize_nonces(payload):
    seen: dict = {}

    def walk(node):
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key in NONCE_COLUMNS and isinstance(value, str):
                    out[key] = f"<nonce#{seen.setdefault(value, len(seen))}>"
                else:
                    out[key] = walk(value)
            return out
        return node

    return walk(payload)


#: Row sets large enough that a verbatim golden would be a multi-megabyte file
#: nobody can read. Reduced to a count plus a digest over the canonical JSON, so
#: equality still detects any drift in any column of any row.
DIGESTED_SECTIONS = (("cache", "quota_window_snapshots"),)


def _digest_rows(rows) -> dict:
    encoded = json.dumps(rows, sort_keys=True, default=str).encode("utf-8")
    return {
        "rowCount": len(rows),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def canonical_view(dump: dict) -> dict:
    """The durable half of a worker dump — what the committed goldens pin."""
    view = _canonicalize_nonces({key: dump.get(key) for key in CANONICAL_KEYS})
    for section, table in DIGESTED_SECTIONS:
        rows = (view.get(section) or {}).get(table)
        if isinstance(rows, list):
            view[section][table] = _digest_rows(rows)
    return view


def tier1_build(**overrides) -> dict:
    build = {"target_lines": TIER1_LINES, "seed_cache": True}
    build.update(overrides)
    return build


def load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text())


def write_golden(name: str, payload: dict) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (GOLDEN_DIR / name).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
