#!/usr/bin/env python3
"""Deterministic synthetic fixture generator for the backend benchmark suite
(issue #276, M3 / Session B).

Writes seeded synthetic ``*.jsonl`` session files under a scratch Claude root
and builds a real ``cache.db`` via the production ``sync_cache`` ingest path, so
the cache has genuine shape (real ``session_entries`` cost rows, the
``conversation_messages`` transcript + FTS, the ``conversation_sessions``
browse-rail rollup, file-touch axes). Benchmarking a hand-forged cache would
measure the wrong thing.

Determinism is SEMANTIC, not byte-level. ``sync_cache`` stamps a few wall-clock
metadata columns during ingest (``session_files.last_ingested_at``, the
``claude_ingest_walk_complete`` marker, ``_ensure_session_files_row``'s
``now_iso``), so ``cache.db`` is NOT byte-identical across builds. Reproducibility
is defined over the SEMANTIC columns of ``session_entries`` /
``conversation_messages`` / ``conversation_sessions`` only — see
``semantic_hash``.

ISOLATION (load-bearing): the generator pins BOTH ``CCTALLY_DATA_DIR`` (scratch
cache/stats dir) AND ``CLAUDE_CONFIG_DIR`` (scratch Claude root) before importing
``cctally``, then re-runs ``_init_paths_from_env()`` so a second build in the same
process re-points ``cache.db`` at the new dir. Pinning only the former would let
``sync_cache`` ingest the operator's REAL ``~/.claude/projects``. This tool never
reads or writes the user's real ``~/.local/share/cctally`` or ``~/.claude``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import random
import sqlite3
import sys

# Three real ids in CLAUDE_MODEL_PRICING (model diversity for the reconcile
# families; priced so ingest emits no unknown-model warnings).
MODELS = ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"]

# Fixed reference epoch — every entry timestamp derives from this, never
# wall-clock, so week/day/month bucketing + reset anchoring stay stable across
# builds and machines.
_REF_EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

# A common searchable token stamped into every message so cross-session search
# and in-conversation find always have hits, plus a varied word pool for
# realistic (deterministic) prose spread.
_SEARCH_TOKEN = "benchmark"
_WORDS = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
    "xray", "yankee", "zulu", "cache", "token", "window", "reset", "usage",
]

# Session C (M5): a graduated turn-count ladder for the `cctally-bench
# --assembly-scan` sweep. One session per rung; each rung emits paired
# user+assistant rows so msg_count ~= 2 * turns. Tunable after the first
# evidence run (a change here busts ONLY the `assembly` scratch cache via the
# marker's params_hash — Codex F5).
ASSEMBLY_TURN_LADDER = [250, 500, 1000, 2000, 4000, 8000]   # full evidence run
ASSEMBLY_TURN_LADDER_SMALL = [10, 40]                        # fast self-test

SCALES = {
    # Tiny — the self-test + fast local iteration.
    "small": {
        "sessions": 8,
        "turns_per_session": 6,
        "large_session_turns": 40,
        "projects": 3,
    },
    # The committed-baseline scale (issue's ~300K-entry target, tuned to a
    # practical build time — see bench/README.md and the committed baseline's
    # dataset_counts for the actual measured shape).
    "large": {
        "sessions": 5000,
        "turns_per_session": 58,
        "large_session_turns": 6000,
        "projects": 12,
    },
    # Session C (M5): one session per ladder rung (NOT uniform sessions). The
    # `ladder` key routes _emit_corpus to the per-session turn list; the marker
    # carries a params_hash over this shape so a ladder edit busts ONLY this
    # scale. Used internally by `cctally-bench --assembly-scan`, never a `--scale`
    # choice for the default suite.
    "assembly": {"ladder": ASSEMBLY_TURN_LADDER, "projects": 3},
    "assembly-small": {"ladder": ASSEMBLY_TURN_LADDER_SMALL, "projects": 2},
}


def _iso(ref_minutes: int) -> str:
    """A deterministic ``…Z`` timestamp = ``_REF_EPOCH + ref_minutes`` (no
    wall-clock)."""
    base = _REF_EPOCH + dt.timedelta(minutes=ref_minutes)
    return base.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _seeded_text(rng: random.Random, kind: str) -> str:
    """Deterministic prose drawn from the fixed word pool, always carrying the
    common search token so every message is findable cross-session."""
    n = rng.randint(6, 18)
    words = " ".join(rng.choice(_WORDS) for _ in range(n))
    return f"{_SEARCH_TOKEN} {kind} {words}"


def emit_session_jsonl(
    path,
    *,
    session_id,
    cwd,
    model,
    seed_rng,
    n_turns,
    base_minute,
    git_branch,
) -> None:
    """Write one session's JSONL rows: paired user + assistant turns in the
    minimal real shape ``_lib_conversation.parse_message_row`` +
    ``_lib_jsonl.parse_cost_entry`` ingest. Each assistant row feeds BOTH
    ``session_entries`` (cost, via ``message.usage`` + ``message.id`` +
    top-level ``requestId``) and ``conversation_messages`` (transcript); each
    user row feeds ``conversation_messages``."""
    path = pathlib.Path(path)
    rows = []
    prev_uuid = None
    for t in range(n_turns):
        u_uuid = f"{session_id}-u{t}"
        rows.append({
            "type": "user",
            "uuid": u_uuid,
            "parentUuid": prev_uuid,
            "sessionId": session_id,
            "timestamp": _iso(base_minute + t * 2),
            "cwd": cwd,
            "gitBranch": git_branch,
            "message": {"role": "user", "content": _seeded_text(seed_rng, "prompt")},
        })
        a_uuid = f"{session_id}-a{t}"
        rows.append({
            "type": "assistant",
            "uuid": a_uuid,
            "parentUuid": u_uuid,
            "sessionId": session_id,
            "timestamp": _iso(base_minute + t * 2 + 1),
            "cwd": cwd,
            "gitBranch": git_branch,
            "requestId": f"{session_id}-req{t}",
            "message": {
                "id": f"{session_id}-msg{t}",
                "role": "assistant",
                "model": model,
                "content": _seeded_text(seed_rng, "assistant"),
                "usage": {
                    "input_tokens": seed_rng.randint(500, 5000),
                    "output_tokens": seed_rng.randint(200, 4000),
                    "cache_read_input_tokens": seed_rng.randint(0, 20000),
                    "cache_creation_input_tokens": seed_rng.randint(0, 3000),
                },
            },
        })
        prev_uuid = a_uuid
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _emit_corpus(projects_dir: pathlib.Path, params: dict, rng: random.Random) -> None:
    """Emit the fixture corpus.

    Two shapes, one code path:
      * uniform (``small``/``large``): ``params['sessions']`` sessions with
        ``turns_per_session`` turns each, session 0 the deliberately-large one.
      * ladder (``assembly``/``assembly-small``, Session C M5): when
        ``params['ladder']`` is present, emit exactly ``len(ladder)`` sessions —
        session ``i`` gets ``ladder[i]`` turns — so the `--assembly-scan` sweep
        has one graduated session per rung. ``sessions`` / ``turns_per_session``
        / ``large_session_turns`` are ignored in this branch.
    Both keep ``session_id=f"sess-{i}"`` and rotate model/project as before."""
    ladder = params.get("ladder")
    if ladder is not None:
        turn_counts = list(ladder)
    else:
        turn_counts = [
            params["large_session_turns"] if i == 0 else params["turns_per_session"]
            for i in range(params["sessions"])
        ]
    for i, n in enumerate(turn_counts):
        model = MODELS[i % len(MODELS)]
        proj = f"proj{i % params['projects']}"
        cwd = f"/bench/{proj}"
        enc = projects_dir / f"-bench-{proj}"
        emit_session_jsonl(
            enc / f"sess-{i}.jsonl",
            session_id=f"sess-{i}",
            cwd=cwd,
            model=model,
            seed_rng=rng,
            n_turns=n,
            base_minute=i * 100,
            git_branch=f"branch-{i % 4}",
        )


def load_cctally():
    """Path-load the extensionless ``bin/cctally`` as module ``"cctally"`` (a
    plain ``import cctally`` can't find an extensionless file). Registered in
    ``sys.modules`` BEFORE exec so the script's own ``_THIS_MODULE`` /
    ``_load_sibling`` back-references resolve to this instance — mirroring the
    codebase's sibling-load idiom. Reused if already loaded."""
    cached = sys.modules.get("cctally")
    if cached is not None:
        return cached
    path = pathlib.Path(__file__).resolve().parent / "cctally"
    loader = importlib.machinery.SourceFileLoader("cctally", str(path))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _pin_env(data_dir: pathlib.Path, claude_dir: pathlib.Path):
    """Pin BOTH env axes + disable dev auto-detect, add bin/ to sys.path, load
    cctally, then re-resolve the path globals so a second build in the same
    process targets THIS data_dir. Returns the ``cctally`` module."""
    os.environ["CCTALLY_DATA_DIR"] = str(data_dir)
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    os.environ.setdefault("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    bin_dir = str(pathlib.Path(__file__).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    cctally = load_cctally()  # MUST follow the env pin (paths captured at import)
    # CCTALLY_DATA_DIR is captured at import; re-run so a repeated call (the
    # determinism test builds two fixtures in one process) re-points
    # APP_DIR/CACHE_DB_PATH/DB_PATH at the new scratch dir.
    cctally._cctally_core._init_paths_from_env()
    return cctally


def _marker_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / ".bench-fixture.json"


def _marker_payload(cctally, *, seed, scale) -> dict:
    try:
        pricing_date = cctally._lib_pricing.PRICING_SNAPSHOT_DATE
    except Exception:
        pricing_date = "unknown"
    payload = {"seed": int(seed), "scale": str(scale), "pricing_date": pricing_date}
    params = SCALES.get(scale, {})
    if "ladder" in params:
        # Session C (M5) / Codex F5: fold a stable hash of the exact ladder +
        # generator shape into the marker so a ladder edit busts the cached
        # JSONL/cache.db for the `assembly` scale ONLY. The small/large markers
        # carry no `ladder` key, so their payloads stay
        # {seed, scale, pricing_date} — Session B's scratch caches + semantic
        # hashes are untouched.
        shape = json.dumps(
            {"ladder": params["ladder"], "projects": params["projects"]},
            sort_keys=True)
        payload["params_hash"] = hashlib.sha256(shape.encode()).hexdigest()[:16]
    return payload


def build_fixture(*, scale: str, seed: int, root) -> pathlib.Path:
    """Build (or reuse) the deterministic synthetic fixture under ``root``.

    Writes JSONL under ``root/claude/projects/**``, pins ``CCTALLY_DATA_DIR`` =
    ``root/data`` + ``CLAUDE_CONFIG_DIR`` = ``root/claude``, builds ``cache.db``
    via ``sync_cache`` and ``conversations.db`` via
    ``sync_claude_conversations``, and returns ``root/data`` (the resolved
    ``CCTALLY_DATA_DIR``). Idempotent: if a marker records a matching
    ``(seed, scale, pricing_date)`` and ``cache.db`` exists, the JSONL-emit +
    ``sync_cache`` are skipped (a ``large`` rebuild is slow), but env is still
    pinned + paths re-resolved so callers can open the cache immediately."""
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; choose from {sorted(SCALES)}")
    root = pathlib.Path(root)
    data_dir = root / "data"
    claude_dir = root / "claude"
    projects = claude_dir / "projects"
    data_dir.mkdir(parents=True, exist_ok=True)
    projects.mkdir(parents=True, exist_ok=True)

    cctally = _pin_env(data_dir, claude_dir)
    want = _marker_payload(cctally, seed=seed, scale=scale)
    marker = _marker_path(data_dir)
    if ((data_dir / "cache.db").exists()
            and (data_dir / "conversations.db").exists()
            and marker.exists()):
        try:
            if json.loads(marker.read_text()) == want:
                return data_dir       # cached hit — nothing to rebuild
        except (OSError, json.JSONDecodeError):
            pass                      # corrupt marker → fall through and rebuild

    _emit_corpus(projects, SCALES[scale], random.Random(seed))
    conn = cctally.open_cache_db()
    try:
        cctally.sync_cache(conn)
    finally:
        conn.close()
    conn = cctally.open_conversations_db()
    try:
        cctally.sync_claude_conversations(conn)
    finally:
        conn.close()
    marker.write_text(json.dumps(want, sort_keys=True))
    return data_dir


def semantic_hash(conn: sqlite3.Connection) -> str:
    """sha256 over the SEMANTIC columns of the three benchmark-relevant tables.

    EXCLUDES every LOCATION / wall-clock artifact so the hash is invariant to
    where the scratch root lives: ``source_path`` / ``line_offset`` /
    ``byte_offset`` (absolute-path + ingest-order dependent) and the wall-clock
    metadata columns (``session_files.last_ingested_at``, the walk-complete
    marker, any ``now_iso`` stamp) never enter the hash. Ordering keys are the
    deterministic content ids (``msg_id`` / ``session_id`` / ``uuid``), not the
    autoincrement id or the path, so two builds under different roots hash
    identically. The one float column (``conversation_sessions.cost_usd``) is
    rounded to neutralize ULP drift."""
    h = hashlib.sha256()
    for sql in (
        "SELECT msg_id, req_id, timestamp_utc, model, input_tokens, "
        "output_tokens, cache_read_tokens, cache_create_tokens "
        "FROM cache_db.session_entries ORDER BY msg_id, req_id",
        "SELECT session_id, uuid, parent_uuid, timestamp_utc, entry_type, text, "
        "model, msg_id FROM conversation_messages ORDER BY session_id, uuid",
        "SELECT session_id, msg_count, ROUND(cost_usd, 6) "
        "FROM conversation_sessions ORDER BY session_id",
    ):
        for row in conn.execute(sql):
            h.update(repr(row).encode())
    return h.hexdigest()


def dataset_counts(conn: sqlite3.Connection) -> dict:
    """``{"sessions", "entries", "messages"}`` row counts for the fixture."""
    def n(q):
        return conn.execute(q).fetchone()[0]
    return {
        "sessions": n("SELECT COUNT(*) FROM conversation_sessions"),
        "entries": n("SELECT COUNT(*) FROM cache_db.session_entries"),
        "messages": n("SELECT COUNT(*) FROM conversation_messages"),
    }


def open_fixture_db(data_dir) -> sqlite3.Connection:
    """Open the split benchmark corpus with compact cache metadata attached."""
    data_dir = pathlib.Path(data_dir)
    conn = sqlite3.connect(data_dir / "conversations.db")
    cache_uri = (data_dir / "cache.db").resolve().as_uri() + "?mode=ro"
    conn.execute("ATTACH DATABASE ? AS cache_db", (cache_uri,))
    return conn


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the deterministic synthetic backend-benchmark fixture."
    )
    ap.add_argument("--scale", choices=sorted(SCALES), default="small")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--out",
        default=None,
        help="scratch root dir (default: a temp dir keyed by scale+seed).",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="rebuild even if a matching cached fixture exists.",
    )
    args = ap.parse_args(argv)
    if args.out:
        root = pathlib.Path(args.out).expanduser()
    else:
        import tempfile
        root = (pathlib.Path(tempfile.gettempdir()) / "cctally-bench"
                / f"{args.scale}-seed{args.seed}")
    if args.force:
        marker = _marker_path(root / "data")
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
    data_dir = build_fixture(scale=args.scale, seed=args.seed, root=root)
    cctally = _pin_env(data_dir, root / "claude")
    conn = cctally.open_conversations_db()
    try:
        counts = dataset_counts(conn)
        digest = semantic_hash(conn)
    finally:
        conn.close()
    print(f"fixture: {data_dir}")
    print(f"scale={args.scale} seed={args.seed}")
    print(f"dataset_counts: {json.dumps(counts)}")
    print(f"semantic_hash: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
