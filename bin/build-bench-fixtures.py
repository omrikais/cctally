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
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import random
import shutil
import sqlite3
import sys
from typing import NamedTuple

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

# Bump when _emit_corpus changes what it writes for a given params dict.
GENERATOR_VERSION = 6

SCALES = {
    # The cheap end of the >=10x pair (#583 S1, spec §7.1). `tiny` and `small`
    # carry the SAME discriminators and differ by 14.3x on the Claude axis and
    # 12.5x on the Codex axis, so a row-count-invariance gate has a pair it can
    # build in the ordinary suite. `large` is the maintainer receipt and is far
    # too slow for that (1m49s, 1.2 GiB) — the pytest phase runs under
    # --timeout=120. Do NOT collapse this pair: implementor 2's gate reads it.
    "tiny": {
        "sessions": 12,
        "turns_per_session": 20,
        "large_session_turns": 80,
        "projects": 3,
        "codex_sessions": 10,
        "codex_events_per_session": 12,
        "codex_accounts": 2,
        "quota_windows": 20,
        "colliding_basename": True,
    },
    # The self-test + fast local iteration profile, at the scale spec §4.2 asks
    # for (roughly 4K Claude / 1.5K Codex entries; §5 calls it a 5.5K-entry
    # corpus). It was 82 Claude + 48 Codex, a 42x shortfall, because the Claude
    # keys predate the spec and the Codex keys were sized to match them.
    "small": {
        "sessions": 40,
        "turns_per_session": 100,
        "large_session_turns": 400,
        "projects": 3,
        # #583 S1 discriminators. Present at EVERY profile with the same
        # structure; only cardinality differs, so a gate passing on `small`
        # exercises the paths the `large` receipt measures.
        "codex_sessions": 30,
        "codex_events_per_session": 50,
        "codex_accounts": 2,
        "quota_windows": 120,
        "colliding_basename": True,
    },
    # The committed-baseline scale (issue's ~300K-entry target, tuned to a
    # practical build time — see bench/README.md and the committed baseline's
    # dataset_counts for the actual measured shape).
    "large": {
        "sessions": 5000,
        "turns_per_session": 58,
        "large_session_turns": 6000,
        "projects": 12,
        "codex_sessions": 1200,
        "codex_events_per_session": 125,
        "codex_accounts": 2,
        "quota_windows": 2400,
        "colliding_basename": True,
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


def _turn_counts(params: dict) -> list[int]:
    """Turns per Claude session for this profile. ONE definition, read by the
    emitter and by ``expected_counts``, so declared and realised cannot drift."""
    ladder = params.get("ladder")
    if ladder is not None:
        return list(ladder)
    return [
        params["large_session_turns"] if i == 0 else params["turns_per_session"]
        for i in range(params["sessions"])
    ]


def _project_dirs(params: dict) -> list[tuple[str, str]]:
    """``[(cwd, encoded_dir_name)]`` for this profile's Claude project roots.

    #583 S1: when ``colliding_basename`` is set, the last two roots are two
    DISTINCT canonical paths whose final segment is identical
    (``/bench/alpha/web`` and ``/bench/beta/web``). ``project`` groups by the
    ``ProjectKey`` object rather than the basename, and nothing in the bench
    corpus used to be able to tell a correct grouping from a basename merge.
    """
    n = max(int(params.get("projects", 1)), 1)
    dirs = [(f"/bench/proj{i}", f"-bench-proj{i}") for i in range(n)]
    if params.get("colliding_basename") and n >= 2:
        dirs[-2:] = [("/bench/alpha/web", "-bench-alpha-web"),
                     ("/bench/beta/web", "-bench-beta-web")]
    return dirs


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
    turn_counts = _turn_counts(params)
    projects = _project_dirs(params)
    for i, n in enumerate(turn_counts):
        model = MODELS[i % len(MODELS)]
        cwd, enc_name = projects[i % len(projects)]
        enc = projects_dir / enc_name
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


# ── #583 S1: the Codex half of the corpus ────────────────────────────────────
# Account attribution is decided per PROVIDER ROOT from that root's auth.json
# (#416), never from a seeded row, so two distinct account keys require two
# configured `$CODEX_HOME` roots rather than one directory with two identities.
_CODEX_IDENTITIES = (
    ("bench-acct-alpha", "alpha@bench.invalid"),
    ("bench-acct-beta", "beta@bench.invalid"),
)
# Priced ids in CODEX_MODEL_PRICING, so ingest emits no unknown-model warning.
_CODEX_MODELS = ("gpt-5.3-codex", "gpt-5.2-codex")
# Recognised by _lib_codex_pools as its own quota pool. Used two ways below:
# as the rollout MODEL (which puts a `modelPool` member in the logical limit
# key) and as a `limit_name` (the second, independent axis).
_CODEX_SPARK_MODEL = "gpt-5.3-codex-spark"
_CODEX_STANDARD_LIMIT_NAME = "Codex weekly limit"
_CODEX_LIMIT_ID = "codex"
_CODEX_SPARK_LIMIT_ID = "codex-spark"
_CODEX_PRIMARY_WINDOW_MINUTES = 330

# ── The corpus clock, and the quota geometry that hangs off it ───────────────
# `_resolve_codex_weekly_cycle` admits a boundary only when its canonical reset
# is STRICTLY AFTER `now`, and it then requires EXACTLY ONE live boundary per
# account — across every slot, not per slot. Two live boundaries resolve
# `conflicting` and none resolves `missing`; either raises CodexCycleUnavailable,
# the source degrades to `availability="partial"` with no hero, and
# `load_cached_rooted_codex_accounting_entries` — the expensive per-cycle read
# this corpus exists to exercise — is NEVER CALLED.
#
# Measured before this geometry: every account owned four live weekly
# boundaries at the frozen clock (conflicting) and none at a real clock
# (missing), so the corpus reached `build.source_bundle` and took its short
# branch at every scale and every clock.
#
# So the geometry is stated, not incidental:
#   * ONE weekly reset, after the clock, reported by every weekly observation.
#   * Every 5h boundary before the clock (they are skipped by the resolver
#     anyway, which considers only 10,080-minute windows, but a live 5h window
#     would be misleading evidence in the envelope).
#   * Every Codex entry inside the live cycle, so the read returns rows.
#
# CONSEQUENCE, for anyone building a gate on this corpus: it carries exactly
# ONE weekly cycle. `validate_corpus` requires a single distinct 10,080-minute
# anchor, so nothing that needs a PRIOR cycle can be exercised here — a reset
# crossing, `blocks` spanning two cycles, milestone carry-over. Those need a
# second anchor and a clock between them, which is a different corpus shape.
# The clock itself is part of the profile contract: read it from
# CORPUS_CLOCK_UTC rather than choosing one per caller.
CORPUS_CLOCK_UTC = _REF_EPOCH + dt.timedelta(days=6)          # 2026-01-07T00:00Z
_CODEX_WEEKLY_LIVE_RESET = _REF_EPOCH + dt.timedelta(days=7)  # 2026-01-08T00:00Z
# Distinct 5h boundaries, all in the eight hours before the clock.
_CODEX_PRIMARY_RESET_SLOTS = 8
# Which session carries the same-identity Spark-versus-standard collision.
# The SECOND root, so the first account's cycle is provably unaffected by it,
# and local index 2 because the smallest root holds exactly three sessions.
# That session would otherwise be the axis-2 carrier; the FIRST root still
# carries axis 2 cleanly, so the corpus keeps both shapes.
_CODEX_COLLISION_ROOT = 1
_CODEX_COLLISION_LOCAL = 2
_CODEX_SECONDARY_WINDOW_MINUTES = 10080


def codex_root_dirs(root, params: dict) -> list[pathlib.Path]:
    """The configured `$CODEX_HOME` provider roots for this profile."""
    n = max(int(params.get("codex_accounts", 0)), 0)
    return [pathlib.Path(root) / f"codex-{i}" for i in range(n)]


def _codex_auth_json(account_id: str, email: str) -> str:
    """The official Codex CLI auth.json shape, with a decodable id_token."""
    def b64(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")

    claims = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
        },
    }
    token = f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(claims)}.sig"
    return json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {"id_token": token, "access_token": "a", "refresh_token": "r"},
        "last_refresh": "2026-01-01T00:00:00Z",
    })


def _codex_session_share(total: int, roots: int) -> list[int]:
    """Split ``total`` sessions across ``roots`` UNEQUALLY, never dropping one.

    Equal shares would let an accidental account merge pass every count check,
    which is the whole reason the corpus carries two accounts. Weights descend
    (2:1 for two roots).

    Every root must receive at least one session, or the two-account
    discriminator silently degrades to one. The previous form gave the whole
    rounding remainder to the first root unconditionally, and a remainder can be
    NEGATIVE once flooring has pushed a small share up to the floor of one:
    ``total=1, roots=2`` produced ``[0, 1]``, so root 0 emitted nothing.
    A negative remainder is now reclaimed from the largest share that can spare
    a session, and a total too small to cover the roots is a profile error
    rather than something to paper over.
    """
    if roots <= 0:
        return []
    if total < roots:
        raise ValueError(
            f"codex_sessions={total} cannot cover {roots} Codex accounts; "
            "every account must emit at least one session")
    weights = [roots - i for i in range(roots)]
    denom = sum(weights)
    shares = [max(total * w // denom, 1) for w in weights]
    remainder = total - sum(shares)
    if remainder >= 0:
        shares[0] += remainder
    else:
        for _ in range(-remainder):
            donor = max(range(roots), key=lambda i: shares[i])
            if shares[donor] <= 1:
                raise ValueError(
                    f"cannot split {total} sessions across {roots} accounts "
                    "without emptying one")
            shares[donor] -= 1
    if sum(shares) != total or min(shares) < 1:
        raise AssertionError(f"bad share split {shares} for {total}/{roots}")
    return shares


class _CodexSessionPlan(NamedTuple):
    """One planned Codex session. Read by the emitter and by expected_counts."""
    root_index: int
    local_index: int
    quota_events: int
    collision: bool


def _codex_emission_plan(params: dict) -> list["_CodexSessionPlan"]:
    """``[(root_index, local_index, quota_bearing_events)]`` for this profile.

    ONE definition, read by the emitter and by ``expected_counts``, so a
    declared count and a realised count cannot drift. It also refuses a profile
    whose declared ``quota_windows`` exceeds what its sessions can carry:
    previously the emitter capped quota events at ``codex_events_per_session``
    and silently under-emitted, with nothing checking the shortfall.
    """
    roots = int(params.get("codex_accounts", 0))
    total = int(params.get("codex_sessions", 0))
    if roots <= 0 or total <= 0:
        return []
    events = max(int(params.get("codex_events_per_session", 1)), 1)
    shares = _codex_session_share(total, roots)
    quota_events = max(int(params.get("quota_windows", 0)), 0) // 2
    base, extra = divmod(quota_events, total)
    # Which session carries the collision, chosen so it EXISTS for any share.
    # A fixed index cannot: the smallest shipped root holds three sessions and
    # a test that shrinks a profile holds two.
    collision_root = min(_CODEX_COLLISION_ROOT, len(shares) - 1)
    collision_local = min(_CODEX_COLLISION_LOCAL, shares[collision_root] - 1)

    plan: list[_CodexSessionPlan] = []
    index = 0
    for root_index, share in enumerate(shares):
        for local in range(share):
            take = base + (1 if index < extra else 0)
            # ONE session in the whole corpus carries the same-identity
            # Spark-versus-standard collision (see _emit_codex_session), and it
            # needs two quota-bearing events to do it: an earlier Spark-labelled
            # observation and a later standard one under one identity.
            collision = (root_index == collision_root
                         and local == collision_local)
            if collision:
                take = max(take, 2)
            if take > events:
                raise ValueError(
                    f"quota_windows={params.get('quota_windows')} needs {take} "
                    f"quota-bearing events per session, but this profile emits "
                    f"only {events}; raise codex_events_per_session or lower "
                    "quota_windows")
            plan.append(_CodexSessionPlan(root_index, local, take, collision))
            index += 1
    return plan


def expected_counts(params: dict) -> dict:
    """The row counts this profile MUST realise, derived from the profile alone.

    `dataset_counts` reports what a build produced; this states what it owed.
    `validate_corpus` compares them, so a generator change that silently emits
    fewer rows is a build failure rather than a quieter benchmark.
    """
    turn_counts = _turn_counts(params)
    plan = _codex_emission_plan(params)
    events = max(int(params.get("codex_events_per_session", 1)), 1)
    return {
        "sessions": len(turn_counts),
        "entries": sum(turn_counts),
        "messages": 2 * sum(turn_counts),
        "codex_entries": len(plan) * events,
        "codex_files": len(plan),
        "quota_windows": 2 * sum(item.quota_events for item in plan),
    }


def _emit_codex_corpus(codex_roots, params: dict, rng: random.Random) -> None:
    """Write auth.json plus seeded rollout JSONL under each provider root.

    Everything reaches the cache through the production ``sync_codex_cache``
    walk — no table is seeded directly — so the corpus exercises the ingest
    path rather than bypassing it. Three properties are deliberate:

    * unequal per-account spend and unequal per-account quota, so a merge is
      visible rather than plausible;
    * both model-pool axes ``bin/_lib_codex_pools.py`` recognises, each fired
      on its own: a rollout whose MODEL is Spark (the `modelPool` member of the
      logical limit key) and a rollout whose native ``limit_name`` is Spark
      while its model is standard. #373 requires the two to be independent;
    * account-level standard quota alongside both, so the classifier has a
      negative case.
    """
    codex_roots = [pathlib.Path(r) for r in codex_roots]
    if not codex_roots:
        return
    plan = _codex_emission_plan(params)
    if not plan:
        return
    events_per_session = max(int(params.get("codex_events_per_session", 1)), 1)
    project_cwds = [cwd for cwd, _enc in _project_dirs(params)]

    for root_index, root in enumerate(codex_roots):
        account_id, email = _CODEX_IDENTITIES[root_index % len(_CODEX_IDENTITIES)]
        root.mkdir(parents=True, exist_ok=True)
        (root / "auth.json").write_text(_codex_auth_json(account_id, email))

    for session_index, item in enumerate(plan):
        root_index, local, quota_here = (
            item.root_index, item.local_index, item.quota_events)
        # Unequal by construction: the first root's windows sit far higher than
        # the second's, so summed used_percent can never coincide.
        used_base = 60.0 - 25.0 * root_index
        _emit_codex_session(
            codex_roots[root_index],
            session_index=session_index,
            local_index=local,
            events=events_per_session,
            quota_events=quota_here,
            used_base=used_base,
            token_scale=3 - root_index,
            cwd=project_cwds[session_index % len(project_cwds)],
            base_minute=_codex_base_minute(
                session_index, len(plan), events_per_session),
            collision=item.collision,
            rng=rng,
        )


def _codex_base_minute(session_index: int, total: int, events: int) -> int:
    """Minutes from the reference epoch to this Codex session's first record.

    Every Codex entry must land INSIDE the live weekly cycle, which runs from
    the epoch to `_CODEX_WEEKLY_LIVE_RESET`, and before `CORPUS_CLOCK_UTC` so
    nothing reads as future evidence. Sessions are therefore spread across that
    span rather than placed at a fixed stride: a fixed stride put `large`'s
    last Codex session 200 days past the epoch, so the cycle read would have
    covered a few percent of the corpus even once it started executing.
    """
    usable = int((CORPUS_CLOCK_UTC - _REF_EPOCH).total_seconds() // 60)
    span = max(usable - (events + 4), 1)
    return (session_index * span) // max(total, 1)


def _emit_codex_session(
    root: pathlib.Path, *, session_index: int, local_index: int, events: int,
    quota_events: int, used_base: float, token_scale: int, cwd: str,
    base_minute: int, collision: bool, rng: random.Random,
) -> None:
    """One rollout file: session_meta, turn_context, then N token_count events."""
    # local_index % 4 selects the pool axis, so both fire inside any root with
    # at least three sessions and neither depends on the other.
    limit_id = _CODEX_LIMIT_ID
    if local_index % 4 == 1:
        model, limit_name = _CODEX_SPARK_MODEL, _CODEX_STANDARD_LIMIT_NAME
    elif local_index % 4 == 2:
        # Axis 2 also gets its OWN limit_id, so it mints its own
        # QuotaWindowIdentity. Sharing the standard identity was destructive
        # rather than merely redundant: `build_history` folds same-identity
        # observations into one history and `codex_history_is_model_scoped`
        # reads the label from that history's single baseline, so whenever the
        # baseline landed on the Spark-labelled observation the ENTIRE history
        # — the account's real weekly quota included — was classified
        # model-scoped and skipped, and the account lost its cycle. Measured on
        # `tiny`, whose smaller root has one standard weekly observation to be
        # outvoted. A separate pool reporting its own limit id is also what the
        # provider does.
        model, limit_name = (
            _CODEX_MODELS[session_index % len(_CODEX_MODELS)], _CODEX_SPARK_MODEL)
        limit_id = _CODEX_SPARK_LIMIT_ID
    else:
        model, limit_name = (
            _CODEX_MODELS[session_index % len(_CODEX_MODELS)],
            _CODEX_STANDARD_LIMIT_NAME)

    session_id = (f"{session_index:08d}-0000-4000-8000-"
                  f"{session_index:012d}")
    day = session_index % 28 + 1
    path = (root / "sessions" / "2026" / "01" / f"{day:02d}"
            / f"rollout-{session_index}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)

    # See the CORPUS_CLOCK_UTC block. Every 5h boundary sits in the eight hours
    # BEFORE the clock, so none of them competes with the weekly cycle; the
    # first session of each root carries the one live weekly boundary and every
    # other session carries a past one. An earlier revision spread resets one
    # day per session, which put `large`'s windows at 2029-04-19 and made
    # `build_codex_source_state` raise outright.
    primary_reset = int((
        CORPUS_CLOCK_UTC
        - dt.timedelta(hours=1 + session_index % _CODEX_PRIMARY_RESET_SLOTS)
    ).timestamp())
    # EVERY weekly observation reports the SAME live reset. Mixing past resets
    # into the weekly slot does not add history, it destroys the cycle: those
    # rows share one QuotaWindowIdentity with the live one (same account, root,
    # slot, limit and window_minutes), so `build_history` folds them into a
    # single history and `select_baseline` picks ONE baseline for it. When that
    # baseline happened to be a past-reset observation, its
    # `canonical_resets_at <= now` and the resolver discarded the account
    # entirely — measured as `availability="partial"` with
    # `codex_cycle_unavailable` even though a live boundary was present in the
    # table. Reporting one reset per week is also what a real provider does.
    secondary_reset = int(_CODEX_WEEKLY_LIVE_RESET.timestamp())

    rows = [
        {"timestamp": _iso(base_minute), "type": "session_meta", "payload": {
            "id": session_id,
            "session_id": session_id,
            "cwd": cwd,
            "model": model,
            "source": "codex",
            "thread_source": "user",
        }},
        {"timestamp": _iso(base_minute + 1), "type": "turn_context",
         "payload": {"model": model}},
    ]
    cumulative = 0
    for k in range(events):
        inp = rng.randint(400, 4000) * token_scale
        out = rng.randint(100, 1500) * token_scale
        cached = rng.randint(0, 8000)
        reasoning = rng.randint(0, 400)
        # The ingest guard drops any token_count whose cumulative total does
        # not strictly exceed the previous one, so this must accumulate.
        cumulative += inp + out
        info = {
            "last_token_usage": {
                "input_tokens": inp,
                "cached_input_tokens": cached,
                "output_tokens": out,
                "reasoning_output_tokens": reasoning,
                "total_tokens": inp + out,
            },
            "total_token_usage": {"total_tokens": cumulative},
        }
        if k < quota_events:
            event_limit_id, event_limit_name = limit_id, limit_name
            if collision:
                # #373 §7.1's baseline-authority shape, realised. Two weekly
                # observations under ONE identity — same limit_id, so the same
                # logical limit key — where the EARLIER one carries the Spark
                # label and the LATER one does not. The rule says the label
                # comes from the baseline rather than from whichever
                # observation was seen first, so a correct reader classifies
                # this history standard and the account's cycle resolves.
                # Giving axis 2 its own limit_id everywhere had removed this
                # shape from the corpus rather than representing it.
                event_limit_id = _CODEX_LIMIT_ID
                event_limit_name = (
                    _CODEX_SPARK_MODEL if k < quota_events - 1
                    else _CODEX_STANDARD_LIMIT_NAME)
            info["rate_limits"] = {
                "credits": None,
                "individual_limit": None,
                "limit_id": event_limit_id,
                "limit_name": event_limit_name,
                "plan_type": "pro",
                "primary": {
                    # Clamped: `quota_window_snapshots` carries
                    # CHECK(used_percent <= 100), and a profile with enough
                    # quota-bearing events per session would otherwise breach it
                    # and fail the ingest rather than the profile.
                    "used_percent": min(round(used_base * 0.5 + k, 3), 100.0),
                    "resets_at": primary_reset,
                    "window_minutes": _CODEX_PRIMARY_WINDOW_MINUTES,
                },
                "rate_limit_reached_type": None,
                "secondary": {
                    "resets_at": secondary_reset,
                    "used_percent": min(round(used_base, 3), 100.0),
                    "window_minutes": _CODEX_SECONDARY_WINDOW_MINUTES,
                },
            }
        rows.append({
            "timestamp": _iso(base_minute + 2 + k),
            "type": "event_msg",
            "payload": {"type": "token_count", "info": info},
        })

    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


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


# Every environment key `_pin_env` writes, including the one it sets by
# `setdefault`. `pinned_env` restores exactly this set, so its promise of
# exactness is literally true rather than approximately so.
PINNED_ENV_KEYS = (
    "CCTALLY_DATA_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "HOME",
    "CCTALLY_DISABLE_DEV_AUTODETECT",
)


@contextlib.contextmanager
def pinned_env(data_dir, claude_dir, codex_dir=None, home_dir=None):
    """Pin the four env axes for the duration of the block, then restore them.

    ``_pin_env`` deliberately leaves the process changed, because a build then
    opens the cache through the pinned paths. That is wrong for a gate: a leaked
    override wins over a later test's HOME-based resolution and points APP_DIR
    at a deleted tmp dir. Every caller that must not change the process uses
    this wrapper instead.

    Restores every key in ``PINNED_ENV_KEYS``, which includes the
    ``CCTALLY_DISABLE_DEV_AUTODETECT`` that ``_pin_env`` sets by ``setdefault``.
    Absence is restored AS absence, not as an empty string.
    """
    saved = {k: os.environ.get(k) for k in PINNED_ENV_KEYS}
    try:
        cctally = _pin_env(pathlib.Path(data_dir), pathlib.Path(claude_dir))
        if codex_dir is not None:
            os.environ["CODEX_HOME"] = str(codex_dir)
        if home_dir is not None:
            os.environ["HOME"] = str(home_dir)
        cctally._cctally_core._init_paths_from_env()
        yield cctally
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        mod = sys.modules.get("cctally")
        if mod is not None:
            try:
                mod._cctally_core._init_paths_from_env()
            except Exception:
                pass


def build_fixture_isolated(*, scale: str, seed: int, root):
    """``build_fixture`` for a caller that must not change the process.

    `build_fixture` pins four environment axes and LEAVES them pinned, which is
    correct for `_main` and `bin/cctally-bench` — both go on to open the cache
    through those paths — and is a defect in a test, because the next test on
    the same pytest-xdist worker then resolves user state through a scratch
    directory that no longer exists. EVERY test caller uses this instead; there
    is no case where a test wants the leak.
    """
    root = pathlib.Path(root)
    # The REAL roots, from the one function that owns their spelling. Passing
    # `root/"codex"` pinned a path no profile ever creates, and `build_fixture`
    # then overwrote it for every Codex profile while leaving the nonexistent
    # path in place for `assembly*`.
    codex_roots = codex_root_dirs(root, SCALES.get(scale, {}))
    codex_pin = ",".join(str(p) for p in codex_roots) if codex_roots else None
    with pinned_env(root / "data", root / "claude", codex_pin, root / "home"):
        return build_fixture(scale=scale, seed=seed, root=root)


def _marker_matches(marker: pathlib.Path, want: dict,
                    data_dir: pathlib.Path) -> bool:
    """Whether a complete, matching corpus is already on disk."""
    if not ((data_dir / "cache.db").exists()
            and (data_dir / "conversations.db").exists()
            and marker.exists()):
        return False
    try:
        return json.loads(marker.read_text()) == want
    except (OSError, json.JSONDecodeError):
        return False  # corrupt marker -> rebuild


def _marker_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / ".bench-fixture.json"


def _marker_payload(cctally, *, seed, scale) -> dict:
    try:
        pricing_date = cctally._lib_pricing.PRICING_SNAPSHOT_DATE
    except Exception:
        pricing_date = "unknown"
    if scale not in SCALES:
        # Falling back to {} gave every unknown scale ONE shared params_hash,
        # so two different unknown scales collided in the marker.
        raise ValueError(f"unknown scale {scale!r}; choose from {sorted(SCALES)}")
    payload = {"seed": int(seed), "scale": str(scale), "pricing_date": pricing_date}
    params = SCALES[scale]
    # Every scale carries a params_hash (#583 S1). Previously only the ladder
    # scales did, so changing `small` or `large` cardinality or provider content
    # left the marker identical and build_fixture reused the stale corpus.
    # GENERATOR_VERSION is folded in so a change to _emit_corpus itself — which
    # no params value reflects — also busts the cache.
    shape = json.dumps(
        {"params": params, "generator": GENERATOR_VERSION}, sort_keys=True)
    payload["params_hash"] = hashlib.sha256(shape.encode()).hexdigest()[:16]
    return payload


# Written into every root this generator builds. `_clear_previous_corpus`
# refuses to delete anything from a non-empty root that lacks it.
_ROOT_SENTINEL = ".bench-fixture-root"


def _write_root_sentinel(root: pathlib.Path) -> None:
    (root / _ROOT_SENTINEL).write_text(
        "Built by bin/build-bench-fixtures.py. This file marks the directory "
        "as generator-owned; the builder refuses to build into, or clear, a "
        "root that lacks it.\n")


def require_generator_owned(root) -> None:
    """Refuse a root this generator did not build. Call BEFORE creating anything.

    `build_fixture` rmtrees the data dir, the Claude projects tree and every
    Codex root, and its `--out` is caller-supplied, so a mistyped path would
    otherwise take whatever sits at those three places with it. Ordering is the
    whole guard: the predicate is only meaningful while the directory still
    holds nothing of ours.
    """
    root = pathlib.Path(root)
    if _is_generator_owned(root):
        return
    raise ValueError(
        f"refusing to build into {root}: it already holds "
        f"{[str(p.name) for p in destroyable_paths(root) if p.exists()]}, "
        f"which this generator would DELETE, and it carries no "
        f"{_ROOT_SENTINEL} sentinel so it did not create them. Point --out at "
        "a new or generator-owned directory.")


def destroyable_paths(root: pathlib.Path) -> list[pathlib.Path]:
    """Exactly what `_clear_previous_corpus` removes from `root`."""
    return [root / "data", root / "claude" / "projects",
            *sorted(p for p in root.glob("codex-*") if p.is_dir())]


def _is_generator_owned(root: pathlib.Path) -> bool:
    """True when `root` is ours to clear.

    Either it carries the sentinel, or it holds NONE of the paths the clear
    would remove. The predicate is scoped to what is actually destroyed rather
    than to "is the directory empty": `bin/cctally-bench-test` legitimately
    passes a `mktemp -d` root into which it has already written its own stderr
    log, and refusing that would have made the guard reject a caller it has no
    quarrel with. An unrelated file in the root is neither read nor deleted.
    """
    if (root / _ROOT_SENTINEL).exists():
        return True
    try:
        return not any(p.exists() for p in destroyable_paths(root))
    except OSError:
        return False


def build_lock_path(root) -> pathlib.Path:
    """The build lock for `root`, deliberately a SIBLING of it.

    `_clear_previous_corpus` rmtrees the data dir, and `cache.db.lock` lives
    inside it — so a lock taken in there is on an inode the next clear unlinks,
    after which two processes hold locks on different inodes and mutual
    exclusion is silently gone. Every caller of `build_fixture` takes this one,
    not only the pytest fixture: the bench and the oracle tool both default to
    fixed machine-global roots and neither used to lock at all.

    What this does NOT close: a process that is not a BUILDER — one merely
    reading the corpus through `open_cache_db` — still takes
    `cache.db.lock` inside the data dir a concurrent clear removes, so the
    unlink-under-a-holder race is narrowed to that case rather than
    eliminated. Accepted deliberately: the corpus is scratch, every reader
    of it in this repo goes through a fixture or a tool that builds first,
    and closing it fully would mean moving a production lock path for a
    test fixture's benefit.
    """
    root = pathlib.Path(root)
    return root.parent / f".{root.name}.build.lock"


def _clear_previous_corpus(root: pathlib.Path, data_dir: pathlib.Path) -> None:
    """Delete what the generator owns under ``root`` before re-emitting.

    A marker MISS used to re-emit on top of the previous corpus, which is not a
    rebuild. The generator names its rollout files deterministically, so a
    content-only change rewrites each file at the same path — and if the new
    bytes are the same LENGTH, the delta ingest sees no growth and skips the
    file entirely, leaving the old rows in `cache.db`. Measured: bounding the
    Codex quota-reset spread changed only integers inside the rollouts, the
    `params_hash` correctly detected the change, `build_fixture` re-emitted, and
    the rebuilt `large` corpus still carried the OLD 2029 reset dates. A marker
    that detects a change is worth nothing if the rebuild it triggers does not.

    Only generator-owned paths are removed, never the whole root, because
    ``--out`` may point at a directory the caller also uses for other things.
    The caller has already refused a root that is not generator-owned, via
    ``require_generator_owned``. That check CANNOT live here: by the time this
    runs, ``build_fixture`` has created ``data/``, ``claude/projects/`` and the
    Codex roots and written the sentinel, so both limbs of the predicate — the
    sentinel and the is-it-empty fallback — are already satisfied by the
    builder's own work. It was measured returning True against a directory
    holding three user files, and this function then deleted them.
    """
    # The WHOLE data dir, not a list of database files. `data/` also holds the
    # append-only journal, and deleting only the databases left retained
    # journal records to be folded back on the next open: a mutated-profile
    # rebuild came back with the right entry counts and 32 quota windows where
    # the profile owed 20. The corpus is fully re-derivable, and `data/` is
    # generator-owned (it IS the pinned CCTALLY_DATA_DIR), so removing it is
    # the honest meaning of "rebuild".
    # `destroyable_paths(root)` is the ONE list, shared with the ownership
    # predicate so the guard and the delete cannot disagree about what is at
    # risk. It hard-codes `root/"data"`, which is what `data_dir` always is —
    # the parameter is kept only to recreate the directory afterwards, and is
    # asserted here rather than left as a latent second spelling.
    if data_dir != root / "data":
        raise ValueError(
            f"data_dir {data_dir} is not {root / 'data'}; the clear list is "
            "derived from the root, so a different data dir would survive it")
    for target in destroyable_paths(root):
        shutil.rmtree(target, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (root / "claude" / "projects").mkdir(parents=True, exist_ok=True)


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
    # FIRST, before a single mkdir: once this function has created `data/` and
    # the provider roots, every ownership predicate is satisfied by its own
    # work and the guard can no longer tell a fresh root from the operator's
    # documents directory.
    require_generator_owned(root)

    data_dir = root / "data"
    claude_dir = root / "claude"
    projects = claude_dir / "projects"
    home_dir = root / "home"
    root.mkdir(parents=True, exist_ok=True)
    _write_root_sentinel(root)
    data_dir.mkdir(parents=True, exist_ok=True)
    projects.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    codex_roots = codex_root_dirs(root, SCALES[scale])
    for codex_root in codex_roots:
        (codex_root / "sessions").mkdir(parents=True, exist_ok=True)

    cctally = _pin_env(data_dir, claude_dir)
    # #583 S1: the other two axes. $CODEX_HOME is COMMA-separated, and two
    # account keys need two configured roots because attribution is decided per
    # root from that root's auth.json. HOME is pinned alongside so nothing
    # resolves user state through the operator's real home.
    if codex_roots:
        os.environ["CODEX_HOME"] = ",".join(str(p) for p in codex_roots)
    os.environ["HOME"] = str(home_dir)
    cctally._cctally_core._init_paths_from_env()
    want = _marker_payload(cctally, seed=seed, scale=scale)
    marker = _marker_path(data_dir)
    if _marker_matches(marker, want, data_dir):
        return data_dir               # cached hit — nothing to rebuild

    # One writer per root, for EVERY caller. `--out` and both tools' defaults
    # are fixed machine-global paths, and a clear that races a concurrent read
    # deletes a corpus out from under it.
    lock_path = build_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Re-check under the lock: another process may have finished the
            # very build this one was about to start.
            if _marker_matches(marker, want, data_dir):
                return data_dir
            _clear_previous_corpus(root, data_dir)
            _write_root_sentinel(root)
            projects.mkdir(parents=True, exist_ok=True)
            _emit_corpus(projects, SCALES[scale], random.Random(seed))
            conn = cctally.open_cache_db()
            try:
                cctally.sync_cache(conn)
            finally:
                conn.close()
            _emit_codex_corpus(
                codex_roots, SCALES[scale], random.Random(seed + 1))
            if codex_roots:
                conn = cctally.open_cache_db()
                try:
                    cctally.sync_codex_cache(conn)
                finally:
                    conn.close()
            conn = cctally.open_conversations_db()
            try:
                cctally.sync_claude_conversations(conn)
            finally:
                conn.close()
            marker.write_text(json.dumps(want, sort_keys=True))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return data_dir


def semantic_hash(conn: sqlite3.Connection) -> str:
    """sha256 over the SEMANTIC columns of the benchmark-relevant tables, ACROSS
    BOTH PROVIDERS.

    EXCLUDES every LOCATION / wall-clock artifact so the hash is invariant to
    where the scratch root lives: ``source_path`` / ``line_offset`` /
    ``byte_offset`` (absolute-path + ingest-order dependent) and the wall-clock
    metadata columns (``session_files.last_ingested_at``, the walk-complete
    marker, any ``now_iso`` stamp) never enter the hash. Ordering keys are the
    deterministic content ids (``msg_id`` / ``session_id`` / ``uuid``), not the
    autoincrement id or the path, so two builds under different roots hash
    identically. Float columns are rounded to neutralize ULP drift.

    #583 S1 added the Codex, account, quota and pool axes, because without them
    two corpora differing in every discriminator the corpus exists to carry
    hashed identically — and the envelope oracle keyed on this hash would then
    prove nothing about the Codex path.

    Two of those axes need a projection rather than the raw column, for the same
    location-invariance reason as above. ``source_root_key`` is derived from the
    provider root's ABSOLUTE path, and ``logical_limit_key`` embeds it, so
    neither can be hashed verbatim; the pool axis enters as the extracted
    ``modelPool`` member and the root axis as a plain count. The ``accounts``
    registry itself is not hashed because it lives in stats.db, which
    ``open_fixture_db`` does not attach — account attribution enters through the
    ``account_key`` column of both entry tables, which is content-derived and
    therefore location-invariant."""
    h = hashlib.sha256()
    for sql in (
        "SELECT msg_id, req_id, timestamp_utc, model, input_tokens, "
        "output_tokens, cache_read_tokens, cache_create_tokens "
        "FROM cache_db.session_entries ORDER BY msg_id, req_id",
        "SELECT session_id, uuid, parent_uuid, timestamp_utc, entry_type, text, "
        "model, msg_id FROM conversation_messages ORDER BY session_id, uuid",
        "SELECT session_id, msg_count, ROUND(cost_usd, 6) "
        "FROM conversation_sessions ORDER BY session_id",
        # Codex accounting. Every hashed column is also an ordering column, so
        # two rows that tie on a prefix cannot swap places between builds.
        "SELECT session_id, account_key, model, timestamp_utc, input_tokens, "
        "cached_input_tokens, output_tokens, reasoning_output_tokens, "
        "total_tokens FROM cache_db.codex_session_entries "
        "ORDER BY 1, 2, 3, 4, 5, 6, 7, 8, 9",
        # Codex quota, with the pool axis as the extracted member.
        "SELECT account_key, "
        "CASE WHEN json_valid(logical_limit_key) "
        "     THEN json_extract(logical_limit_key, '$.modelPool') END, "
        "limit_id, limit_name, observed_slot, window_minutes, "
        "ROUND(used_percent, 6), resets_at_utc, canonical_resets_at_utc, "
        "plan_type, reached_type, observed_model, captured_at_utc "
        "FROM cache_db.quota_window_snapshots "
        "ORDER BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13",
        # The root axis as a location-invariant cardinality.
        "SELECT (SELECT COUNT(*) FROM cache_db.codex_source_roots), "
        "(SELECT COUNT(*) FROM cache_db.codex_session_files)",
    ):
        for row in conn.execute(sql):
            h.update(repr(row).encode())
    return h.hexdigest()


def dataset_counts(conn: sqlite3.Connection) -> dict:
    """Row counts for the fixture, across both providers (#583 S1)."""
    def n(q):
        return conn.execute(q).fetchone()[0]
    return {
        "sessions": n("SELECT COUNT(*) FROM conversation_sessions"),
        "entries": n("SELECT COUNT(*) FROM cache_db.session_entries"),
        "messages": n("SELECT COUNT(*) FROM conversation_messages"),
        "codex_entries": n("SELECT COUNT(*) FROM cache_db.codex_session_entries"),
        "codex_files": n("SELECT COUNT(*) FROM cache_db.codex_session_files"),
        "quota_windows": n("SELECT COUNT(*) FROM cache_db.quota_window_snapshots"),
    }


def _attached_cache_path(conn: sqlite3.Connection) -> str:
    """The file backing the ``cache_db`` attachment on an open_fixture_db conn."""
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "cache_db" and path:
            return path
    raise ValueError("no cache_db attachment on this connection")


def _live_weekly_accounts(conn: sqlite3.Connection) -> dict:
    """``{account_key: {(window_minutes, resets_at)}}`` a READER would resolve.

    Runs the same three kernel calls `_resolve_codex_weekly_cycle` runs —
    `build_history`, `select_baseline`, `codex_history_is_model_scoped` — and
    applies the same liveness predicate, so this reports what the product will
    classify rather than what the rows look like. Kernel-only: no second home
    for #373's rule, and no reimplementation of the resolver's ranking (the
    fresh/stale split is not repeated here, because both states are eligible
    and this asserts eligibility, not which one wins).
    """
    import _lib_codex_pools
    import _lib_quota

    cctally = sys.modules.get("cctally")
    if cctally is None:
        raise ValueError("cctally must be loaded before validate_corpus")
    load = cctally._cctally_quota.load_codex_quota_observations

    cache_conn = sqlite3.connect(
        "file:" + _attached_cache_path(conn) + "?mode=ro", uri=True)
    try:
        observations = load(cache_conn=cache_conn)
    finally:
        cache_conn.close()

    live: dict = {}
    for history in _lib_quota.build_history(tuple(observations)):
        if history.identity.window_minutes != 10_080:
            continue
        baseline = _lib_quota.select_baseline(
            history.observations, CORPUS_CLOCK_UTC)
        if _lib_codex_pools.codex_history_is_model_scoped(
                history, baseline=baseline):
            continue
        if baseline is None or baseline.canonical_resets_at <= CORPUS_CLOCK_UTC:
            continue
        account = history.identity.account_key
        if account is None:
            continue
        live.setdefault(account, set()).add(
            (history.identity.window_minutes,
             _codex_iso(baseline.canonical_resets_at)))
    return live


def _codex_iso(value) -> str:
    # Tolerates None so a caller that passed NO clock — the exact defect the
    # rendered-tick gate exists to catch — gets that gate's message rather than
    # an AttributeError from its formatting.
    if value is None:
        return "no clock (wall clock)"
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def assert_tick_renders_the_codex_cycle(cctally, now_utc) -> dict:
    """Build one real tick and REFUSE a degraded Codex leg.

    `validate_corpus` cannot catch this. It compares stored anchors against
    `CORPUS_CLOCK_UTC`, which is a property of the corpus at an instant, so it
    passes identically whether the caller then renders at that clock or at the
    wall clock. A consumer that forgets the clock therefore measures the short
    branch with every gate green — which happened to `bin/cctally-bench`, in
    the round whose headline defect was that very branch.

    So this asserts the RENDERED tick: pass the clock you are about to measure
    with, and a caller that passes the wrong one fails here instead of quietly
    recording a cheaper number.

    Returns the realised cycle summary. Raises ValueError on a degraded leg.
    """
    snapshot = cctally._cctally_tui._tui_build_snapshot(
        now_utc=now_utc, skip_sync=True, precompute_envelope=True,
        runtime_bind="127.0.0.1",
    )
    return assert_snapshot_renders_the_codex_cycle(snapshot, now_utc)


def assert_snapshot_renders_the_codex_cycle(snapshot, now_utc) -> dict:
    """The same refusal, over a snapshot the caller ALREADY built.

    A caller that is measuring must use this form. The building variant warms
    process-level memos, and a tool with no cold reset before its first timed
    build then records a warm one: `bin/cctally-snapshot-measure` reported a
    0.00s "cold build" the moment the building gate was installed ahead of it.
    """
    bundle = getattr(snapshot, "source_bundle", None)
    if bundle is None:
        raise ValueError("the tick published no source bundle")
    codex = (bundle.sources or {}).get("codex")
    if codex is None:
        raise ValueError("the tick published no Codex source")
    warnings = tuple(getattr(w, "code", w)
                     for w in (getattr(codex, "warnings", None) or ()))
    availability = getattr(codex, "availability", None)
    if "codex_cycle_unavailable" in warnings or availability != "ok":
        raise ValueError(
            f"the Codex leg rendered DEGRADED at {_codex_iso(now_utc)}: "
            f"availability={availability!r} warnings={list(warnings)}. The "
            "per-cycle accounting read did not run, so whatever this measured "
            "is the short branch. Pass CORPUS_CLOCK_UTC as `now_utc`.")
    hero = (getattr(codex, "data", None) or {}).get("hero") or {}
    if not hero.get("cycle") or not (hero.get("total_tokens") or 0) > 0:
        raise ValueError(
            f"the Codex cycle resolved at {_codex_iso(now_utc)} but carries no "
            f"spend, so it covers none of the corpus: {hero.get('cycle')!r}")
    return {"availability": availability, "cycle": hero.get("cycle"),
            "total_tokens": hero.get("total_tokens")}


def validate_corpus(conn: sqlite3.Connection, scale: str) -> dict:
    """Assert the built corpus matches its profile EXACTLY, then return counts.

    Two claims, both required by spec §5 before the `large` receipt measures
    anything. First that every count is exactly what the profile owed, not
    merely non-zero — a generator change that emits fewer rows must fail the
    build rather than quietly produce a cheaper benchmark. Second that every
    discriminator is REALISED in the database, because a profile can declare
    all three and emit none: the emission plan is cardinality-dependent, so a
    profile with too few sessions per Codex root reaches only one pool axis.

    Raises ``ValueError`` naming what is missing. The connection must be an
    ``open_fixture_db`` handle, so ``cache_db`` is attached.
    """
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}")
    params = SCALES[scale]
    want = expected_counts(params)
    got = dataset_counts(conn)
    if got != want:
        wrong = {k: (want[k], got.get(k)) for k in want if got.get(k) != want[k]}
        raise ValueError(
            f"corpus {scale!r} does not match its profile (expected, got): {wrong}")
    if not params.get("codex_accounts"):
        return got

    def rows(sql, params=()):
        return [r for r in conn.execute(sql, params)]

    accounts = rows("SELECT DISTINCT account_key FROM cache_db.codex_session_entries "
                    "WHERE account_key IS NOT NULL")
    if len(accounts) < 2:
        raise ValueError(f"corpus {scale!r} realised {len(accounts)} Codex "
                         "account(s); the discriminator needs two")
    spend = {k: v for k, v in rows(
        "SELECT account_key, SUM(input_tokens + output_tokens) "
        "FROM cache_db.codex_session_entries WHERE account_key IS NOT NULL "
        "GROUP BY account_key")}
    if len(set(spend.values())) != len(spend):
        raise ValueError(f"corpus {scale!r} has equal per-account spend {spend}; "
                         "an account merge would be invisible")
    quota = {k: v for k, v in rows(
        "SELECT account_key, SUM(used_percent) FROM cache_db.quota_window_snapshots "
        "WHERE account_key IS NOT NULL GROUP BY account_key")}
    if len(quota) < 2 or len(set(quota.values())) != len(quota):
        raise ValueError(f"corpus {scale!r} has equal or single-account quota "
                         f"{quota}; an account merge would be invisible")

    import _lib_codex_pools

    windows = rows("SELECT logical_limit_key, limit_name "
                   "FROM cache_db.quota_window_snapshots")
    by_key = [w for w in windows if _lib_codex_pools._key_has_model_pool(w[0])]
    by_name = [w for w in windows
               if not _lib_codex_pools._key_has_model_pool(w[0])
               and _lib_codex_pools.codex_model_scoped_quota_pool(w[1])]
    standard = [w for w in windows
                if not _lib_codex_pools.is_model_scoped_codex_quota(w[0], w[1])]
    if not by_key:
        raise ValueError(f"corpus {scale!r} realised no window carrying a "
                         "modelPool member in its logical limit key")
    if not by_name:
        raise ValueError(f"corpus {scale!r} realised no window carrying a Spark "
                         "limit_name on its own")
    if not standard:
        raise ValueError(f"corpus {scale!r} realised no account-level standard "
                         "quota window, so the classifier has no negative case")

    # The live weekly cycle must RESOLVE. Everything above can hold while
    # `_resolve_codex_weekly_cycle` still raises, and then the per-cycle
    # accounting read this corpus exists to exercise is never called at all.
    # Asserted on the stored anchors rather than by building a snapshot, so the
    # generator keeps no dependency on the dashboard source layer; the realised
    # end-to-end property is asserted by
    # tests/test_bench_corpus_discriminators.py against a real build.
    # The REALISED classification, through the same three kernel calls a reader
    # makes: build_history -> select_baseline -> codex_history_is_model_scoped.
    #
    # A row-by-row shape check cannot stand in for it. The previous form asked
    # only whether each account still had one live non-model-scoped ROW, and a
    # reader classifies a HISTORY: swapping the two collision observations'
    # labels, so the Spark-labelled one arrives last, makes BOTH of that
    # account's weekly histories model-scoped — the account has no live cycle
    # at all — while every row-level check passes unchanged. The tick gate
    # cannot see it either, because `_resolve_codex_weekly_cycle` degrades only
    # when NO account resolves. This is the property; assert the property.
    per_account = _live_weekly_accounts(conn)
    expected_accounts = int(params.get("codex_accounts", 0))
    if len(per_account) != expected_accounts:
        raise ValueError(
            f"corpus {scale!r} retains a live, non-model-scoped weekly history "
            f"for {len(per_account)} account(s) at "
            f"{_codex_iso(CORPUS_CLOCK_UTC)}, not {expected_accounts}: "
            f"{sorted(per_account)}. An account whose weekly history is "
            "classified model-scoped has no cycle, and the aggregate tick "
            "still renders 'ok' because another account resolves.")
    wrong = {k: sorted(v) for k, v in per_account.items() if len(v) != 1}
    if wrong:
        raise ValueError(
            f"corpus {scale!r} must expose EXACTLY ONE live weekly boundary per "
            f"account at {_codex_iso(CORPUS_CLOCK_UTC)}; these accounts expose "
            f"another count, which resolves 'conflicting': {wrong}")

    # Window VARIETY, not just row counts. Rows are observations; a corpus can
    # hold thousands of them against one window and exercise nothing.
    anchors = rows(
        "SELECT window_minutes, "
        "COUNT(DISTINCT COALESCE(canonical_resets_at_utc, resets_at_utc)) "
        "FROM cache_db.quota_window_snapshots GROUP BY window_minutes")
    geometry = {int(wm): int(n) for wm, n in anchors}
    if geometry.get(10080, 0) != 1:
        raise ValueError(
            f"corpus {scale!r} must hold exactly one weekly anchor "
            f"(the live one); it holds {geometry.get(10080, 0)}")
    if geometry.get(330, 0) < 2:
        raise ValueError(
            f"corpus {scale!r} holds {geometry.get(330, 0)} distinct 5h "
            "anchors; the 5h axis needs more than one to be a variety at all")

    # #373 §7.1's baseline-authority shape must be REALISED: one logical limit
    # key carrying both a Spark and a non-Spark label. Giving axis 2 its own
    # limit_id everywhere removed this from the corpus rather than
    # representing it.
    # Classification through the KERNEL again. The SQL this replaced
    # (`limit_name LIKE '%codex-spark%'`) dropped the kernel's leading hyphen
    # and leaned on SQLite's ASCII-only LIKE folding, so the two disagreed on
    # inputs such as a bare "codex-spark" — a second #373 one-home violation,
    # created inside the fix for the first, forty lines below it.
    labelled: dict = {}
    for key, label in rows(
            "SELECT logical_limit_key, limit_name "
            "FROM cache_db.quota_window_snapshots WHERE account_key IS NOT NULL"):
        spark = _lib_codex_pools.codex_model_scoped_quota_pool(label) is not None
        seen = labelled.setdefault(key, {"spark": False, "standard": False})
        seen["spark" if spark else "standard"] = True
    collided = [
        key for key, seen in labelled.items()
        # The KEY itself must not be model-scoped. A collision on a key that
        # already carries a `modelPool` member is inert: both observations are
        # model-scoped by the key axis, the baseline-authority path is never
        # reached, and the shape proves nothing. Reachable — the collision's
        # local index is clamped to the smaller root's size, and index 1 is the
        # Spark-MODEL axis.
        if seen["spark"] and seen["standard"]
        and not _lib_codex_pools._key_has_model_pool(key)
    ]
    if not collided:
        raise ValueError(
            f"corpus {scale!r} realises no LIVE same-identity Spark-versus-"
            "standard weekly collision on a key that is not already "
            "model-scoped, so nothing exercises the rule that the pool label "
            "comes from the baseline observation")

    roots = [r[0] for r in rows("SELECT DISTINCT project_path FROM "
                                "cache_db.session_files WHERE project_path IS NOT NULL")]
    basenames = [r.rstrip("/").rsplit("/", 1)[-1] for r in roots]
    if not [b for b in set(basenames) if basenames.count(b) >= 2]:
        raise ValueError(f"corpus {scale!r} realised no two distinct project "
                         f"roots sharing a basename: {sorted(roots)}")
    return got


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
