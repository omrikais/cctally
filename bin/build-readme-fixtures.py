#!/usr/bin/env python3
"""Build the marketing SQLite fixture for the public README screenshots.

Writes a fake-home tree at the requested out_dir so HOME=<scratch>/home
resolves the production layout (~/.local/share/cctally/{stats.db,cache.db,
config.json}).

Content (per docs/superpowers/specs/2026-05-05-public-readme-design.md):
- 8 weeks of weekly_usage_snapshots + weekly_cost_snapshots with a gentle
  upward trend
- session_entries spanning the current week across 4 projects (web-app,
  api-gateway, data-pipeline, mobile-client) with Sonnet/Opus/Haiku mix
- 5h block data for the current week and a few prior windows; the open
  block's `five_hour_window_key` is mirrored onto the latest
  weekly_usage_snapshots row so the dashboard's current-week panel can
  bind to a non-null `current_week.five_hour_block` (CLAUDE.md gotcha:
  "Dashboard `current_week.five_hour_block` binds to the latest
  snapshot's `five_hour_window_key`...").
- Three current-week weekly_usage_snapshots rows (as_of, as_of-12h,
  as_of-24h) so the forecast modal lands on `confidence: high` and
  surfaces a recent-24h projection.
- percent_milestones rows so the TUI hero shot displays crossings
- config.json pinning `display.tz = "America/Los_Angeles"` so the
  dashboard + CLI render dates in LA time regardless of host TZ
  (otherwise screenshots inherit the maintainer's IDT).

Today-anchored: --as-of (default: today UTC) shifts every date so screenshots
don't visibly age.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    seed_session_entry,
    seed_session_file,
    seed_weekly_usage_snapshot,
    stamp_all_stats_migrations_applied,
)
from _cctally_cache import _recompute_conversation_sessions  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "tests/fixtures/readme/home/.local/share/cctally"
DEFAULT_TUI_SNAPSHOT = REPO_ROOT / "tests/fixtures/readme/tui_snapshot.py"

PROJECTS = ("web-app", "api-gateway", "data-pipeline", "mobile-client")
# Named model constants — referenced directly by the conversation seeder
# (so its chips don't depend on positional indices) and assembled into the
# `MODELS` set + `WALK_CYCLE` that drive the 30-day usage walk and the
# dashboard / five-hour-blocks per-row model breakdowns.
SONNET_MODEL = "claude-sonnet-4-6"
OPUS_MODEL = "claude-opus-4-8"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
FABLE_MODEL = "claude-fable-5"
# Canonical set of models this fixture exercises (documentation/order).
MODELS = (SONNET_MODEL, OPUS_MODEL, HAIKU_MODEL, FABLE_MODEL)
# The three established models rotate evenly across the usage walk (this is
# the original, cost-tuned assignment).
BASE_CYCLE = (SONNET_MODEL, OPUS_MODEL, HAIKU_MODEL)


def DEFAULT_AS_OF_FN() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_conversation_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    uuid: str,
    parent_uuid: Optional[str],
    source_path: str,
    byte_offset: int,
    timestamp_utc: str,
    entry_type: str,
    text: str = "",
    blocks_json: str = "[]",
    model: Optional[str] = None,
    msg_id: Optional[str] = None,
    req_id: Optional[str] = None,
    cwd: Optional[str] = None,
    git_branch: Optional[str] = None,
    is_sidechain: int = 0,
    source_tool_use_id: Optional[str] = None,
    stop_reason: Optional[str] = None,
    attribution_skill: Optional[str] = None,
    attribution_plugin: Optional[str] = None,
) -> None:
    """Insert one ``conversation_messages`` row.

    Mirrors the helper in ``bin/build-conversation-fixtures.py`` so the
    marketing fixture can seed synthetic transcripts the dashboard's
    read-only conversation reader will render. The AFTER INSERT FTS trigger
    (created by ``_apply_cache_schema``) indexes ``text`` automatically when
    FTS5 is available; ``UNIQUE(source_path, byte_offset)`` mirrors prod.
    """
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id, uuid, parent_uuid, source_path, byte_offset, "
        " timestamp_utc, entry_type, text, blocks_json, model, msg_id, req_id, "
        " cwd, git_branch, is_sidechain, source_tool_use_id, "
        " stop_reason, attribution_skill, attribution_plugin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id, uuid, parent_uuid, source_path, byte_offset,
            timestamp_utc, entry_type, text, blocks_json, model, msg_id, req_id,
            cwd, git_branch, is_sidechain, source_tool_use_id,
            stop_reason, attribution_skill, attribution_plugin,
        ),
    )


# --- Synthetic transcript content for the conversation-viewer screenshot ----
# All fictional and safe to publish. The hero (api-gateway) session is a full
# 16-turn token-bucket-rate-limiter flow exercising Grep/Read/Write/Edit/Bash
# tool cards, a thinking block, and a Haiku subagent thread; three shorter
# filler sessions populate the rail.
_HERO_GREP_RESULT = """src/middleware/index.ts:1: export { authHandler } from './auth-handler';
src/middleware/auth-handler.ts:12: export const authHandler = (req, res, next) => {
src/index.ts:34: app.use('/api/v1', authHandler, v1Router);
src/index.ts:35: app.use('/api/legacy', authHandler, legacyRouter);
src/middleware/error-handler.ts:4: export const errorHandler = (req, res, next) => {"""

_HERO_AUTH_TS = """import { Request, Response, NextFunction } from 'express';

export interface AuthContext {
  userId: string;
  userTier: 'free' | 'pro' | 'enterprise';
  email: string;
  permissions: string[];
}

export const authHandler = (req: Request, res: Response, next: NextFunction) => {
  const token = req.headers.authorization?.split(' ')[1];
  if (!token) return res.status(401).json({ error: 'Missing token' });
  const payload = verifyToken(token);
  (req as any).auth = {
    userId: payload.sub,
    userTier: payload.tier || 'free',
    email: payload.email,
    permissions: payload.scopes,
  } as AuthContext;
  next();
};"""

_HERO_RATELIMITER_TS = """import { Request, Response, NextFunction } from 'express';
import { AuthContext } from './auth-handler';

interface TokenBucket {
  tokens: number;
  lastRefillTime: number;
}

const TIER_CONFIG: Record<string, { capacity: number; refillRate: number }> = {
  free: { capacity: 100, refillRate: 100 / 60 },
  pro: { capacity: 500, refillRate: 500 / 60 },
  enterprise: { capacity: 5000, refillRate: 5000 / 60 },
};

export class RateLimiter {
  private buckets: Map<string, TokenBucket> = new Map();

  tryConsume(userId: string, tier: string, tokensNeeded = 1): boolean {
    const now = Date.now();
    const config = TIER_CONFIG[tier] || TIER_CONFIG.free;
    let bucket = this.buckets.get(userId);
    if (!bucket) {
      bucket = { tokens: config.capacity, lastRefillTime: now };
      this.buckets.set(userId, bucket);
    }
    const elapsed = (now - bucket.lastRefillTime) / 1000;
    bucket.tokens = Math.min(config.capacity, bucket.tokens + elapsed * config.refillRate);
    bucket.lastRefillTime = now;
    if (bucket.tokens >= tokensNeeded) {
      bucket.tokens -= tokensNeeded;
      return true;
    }
    return false;
  }

  middleware = (req: Request, res: Response, next: NextFunction) => {
    const auth = (req as any).auth as AuthContext | undefined;
    if (!auth) return next();
    if (!this.tryConsume(auth.userId, auth.userTier)) {
      return res.status(429).json({ error: 'Rate limit exceeded', retryAfter: 60 });
    }
    next();
  };
}"""

_HERO_EDIT_DIFF = """--- a/src/index.ts
+++ b/src/index.ts
@@ -1,3 +1,4 @@
 import { authHandler } from './middleware/auth-handler';
+import { RateLimiter } from './middleware/rate-limiter';
 import { v1Router } from './routes/v1';
@@ -32,8 +33,9 @@ app.use(errorHandler);
+const limiter = new RateLimiter();
-app.use('/api/v1', authHandler, v1Router);
-app.use('/api/legacy', authHandler, legacyRouter);
+app.use('/api/v1', authHandler, limiter.middleware, v1Router);
+app.use('/api/legacy', authHandler, limiter.middleware, legacyRouter);"""

_HERO_TEST_OUTPUT = """PASS  src/middleware/__tests__/rate-limiter.test.ts
  RateLimiter
    ✓ should allow requests within free tier limit (45ms)
    ✓ should reject requests over free tier limit (12ms)
    ✓ should allow pro tier higher limits (28ms)
    ✓ should refill tokens over time (89ms)
    ✓ should handle concurrent users independently (31ms)
    ✓ should return 429 when rate limited (19ms)
    ✓ should include retryAfter header in 429 response (22ms)

Test Suites: 1 passed, 1 total
Tests:       7 passed, 7 total
Time:        1.234 s"""


def _populate_conversations(
    cache_conn: sqlite3.Connection, *, as_of: dt.datetime
) -> None:
    """Seed synthetic ``conversation_messages`` for the conversation-viewer
    screenshot, attached to the SAME session ids the fixture already seeds so
    the rail is coherent with the rest of the marketing data.

    Direct-INSERT (mirrors ``build-conversation-fixtures.py``) — no JSONL on
    disk, no reingest flags. Deliberately seeds NO new ``session_entries``:
    the carefully-tuned current-week spend ($28.13 / 53% / ~104% forecast)
    must not move, and the reader renders thinking blocks, tool cards, the
    subagent thread, and model chips from ``conversation_messages`` alone —
    per-turn cost simply renders absent. Caller must invoke
    ``_recompute_conversation_sessions`` afterward to build the rail rollup.
    """
    home = REPO_ROOT / "tests/fixtures/readme/home"
    OPUS, SONNET, HAIKU, FABLE = OPUS_MODEL, SONNET_MODEL, HAIKU_MODEL, FABLE_MODEL
    offs: dict[str, int] = {}

    def emit(
        *, sid, uuid, parent, etype, when, source, cwd, branch,
        text="", blocks=None, model=None, msg_id=None, req_id=None,
        sidechain=0,
    ):
        off = offs.get(source, 0)
        offs[source] = off + 1
        _insert_conversation_message(
            cache_conn, session_id=sid, uuid=uuid, parent_uuid=parent,
            source_path=source, byte_offset=off, timestamp_utc=_iso(when),
            entry_type=etype, text=text,
            blocks_json=json.dumps(blocks) if blocks is not None else "[]",
            model=model, msg_id=msg_id, req_id=req_id, cwd=cwd,
            git_branch=branch, is_sidechain=sidechain,
        )

    def tool_use(name, tid, *, preview, inp, subagent_type=None):
        b = {"kind": "tool_use", "name": name, "id": tid,
             "input": inp, "input_summary": json.dumps(inp),
             "input_truncated": False, "preview": preview}
        if subagent_type:
            b["subagent_type"] = subagent_type
        return b

    def tool_result(tid, text, *, agent_id=None, meta=None, is_error=False):
        b = {"kind": "tool_result", "text": text, "truncated": False,
             "is_error": is_error, "tool_use_id": tid}
        if agent_id:
            b["agent_id"] = agent_id
        if meta:
            b["subagent_meta"] = meta
        return b

    # Per-turn cost so the reader / rail / outline show real $ + tokens
    # (rather than $0.00 / 0 tokens). High line_offsets keep these clear of
    # the 30-day walk's entries on the same source files. Timestamps land in
    # each conversation's own window, so the small added spend (~$1.5 total)
    # stays coherent with the tuned current-week figures and leaves the
    # usage-% / forecast (snapshot-driven) untouched.
    loff = [900000]

    def cost(source, mid, rid, model, when, *, inp, out, cc=0, cr=0):
        seed_session_entry(
            cache_conn, source_path=source, line_offset=loff[0],
            timestamp_utc=_iso(when), model=model,
            input_tokens=inp, output_tokens=out,
            cache_create=cc, cache_read=cr, msg_id=mid, req_id=rid)
        loff[0] += 1

    # ===== HERO SESSION: api-gateway token-bucket rate limiter =============
    proj = "api-gateway"
    sid = f"sess-{proj}-00"
    src = f"{home}/.claude/projects/-{proj}/sess-{proj}-00.jsonl"
    cwd = f"/Users/dev/code/{proj}"
    branch = "feat/rate-limiting"
    agent_hash = "7c3f1a08"
    agent_src = f"{home}/.claude/projects/-{proj}/agent-{agent_hash}.jsonl"
    # size_bytes=0 keeps this synthetic file out of the orphan-tracked-files
    # scan (sync_cache only flags non-zero-size tracked paths).
    seed_session_file(cache_conn, path=agent_src, session_id=sid,
                      project_path=cwd, size_bytes=0, last_byte_offset=0)
    b0 = as_of - dt.timedelta(minutes=42)

    def H(i):
        return b0 + dt.timedelta(minutes=i)

    auth_path = f"{cwd}/src/middleware/auth-handler.ts"
    rl_path = f"{cwd}/src/middleware/rate-limiter.ts"
    idx_path = f"{cwd}/src/index.ts"

    emit(sid=sid, uuid="hg00", parent=None, etype="human", when=H(0),
         source=src, cwd=cwd, branch=branch,
         text=("We need to add per-user rate limiting to the gateway "
               "middleware to prevent abuse from high-volume clients. The "
               "limiter should respect user tier (free/pro/enterprise) and "
               "reject requests when limits are exceeded."))
    emit(sid=sid, uuid="hg01", parent="hg00", etype="assistant", when=H(1),
         source=src, cwd=cwd, branch=branch, model=OPUS, msg_id="mh1",
         req_id="rh1", blocks=[
             {"kind": "thinking", "text": (
                 "I'll locate the existing middleware, understand the auth "
                 "layer where the user tier lives, then add a token-bucket "
                 "rate limiter that sits right after auth so it has user "
                 "context. In-memory buckets for now, with a note to move to "
                 "Redis for distributed setups, and per-tier limits.")},
             tool_use("Grep", "tu_grep", preview="middleware in src/",
                      inp={"pattern": "middleware", "path": "src/",
                           "glob": "*.ts"})])
    emit(sid=sid, uuid="hg02", parent="hg01", etype="tool_result", when=H(2),
         source=src, cwd=cwd, branch=branch,
         blocks=[tool_result("tu_grep", _HERO_GREP_RESULT)])
    emit(sid=sid, uuid="hg03", parent="hg02", etype="assistant", when=H(3),
         source=src, cwd=cwd, branch=branch, model=OPUS, msg_id="mh2",
         req_id="rh2", blocks=[
             tool_use("Read", "tu_read", preview="src/middleware/auth-handler.ts",
                      inp={"file_path": auth_path})])
    emit(sid=sid, uuid="hg04", parent="hg03", etype="tool_result", when=H(4),
         source=src, cwd=cwd, branch=branch,
         blocks=[tool_result("tu_read", _HERO_AUTH_TS)])
    plan = ("The auth handler already populates `req.auth` with the user tier "
            "(free/pro/enterprise), so I'll add a token-bucket rate limiter "
            "right after the auth middleware. It keeps per-user buckets keyed "
            "by userId, with capacity and refill rate set per tier — free 100 "
            "req/min, pro 500 req/min, enterprise 5000 req/min — and rejects "
            "with a 429 once the bucket is empty. Let me confirm every call "
            "site first.")
    emit(sid=sid, uuid="hg05", parent="hg04", etype="assistant", when=H(5),
         source=src, cwd=cwd, branch=branch, model=OPUS, msg_id="mh3",
         req_id="rh3", text=plan, blocks=[
             {"kind": "text", "text": plan},
             tool_use("Task", "tu_task", preview="Find authHandler call sites",
                      subagent_type="Explore",
                      inp={"description": "Find authHandler call sites",
                           "subagent_type": "Explore",
                           "prompt": ("Search the api-gateway codebase for all "
                                      "route files that mount or use the "
                                      "authHandler middleware; return the file "
                                      "paths and how it's attached.")})])
    # --- subagent thread (own agent-<hash>.jsonl, is_sidechain=1, Haiku) ---
    emit(sid=sid, uuid="sub_h1", parent="hg05", etype="human", when=H(6),
         source=agent_src, cwd=cwd, branch=branch, sidechain=1,
         text=("Search the api-gateway codebase for all route files that mount "
               "or use the authHandler middleware; return the file paths and "
               "how it's attached."))
    emit(sid=sid, uuid="sub_a1", parent="sub_h1", etype="assistant", when=H(7),
         source=agent_src, cwd=cwd, branch=branch, sidechain=1, model=HAIKU,
         msg_id="ms1", req_id="rs1",
         text=("Found three mount points across the route layer."),
         blocks=[
             {"kind": "thinking", "text": (
                 "I'll grep for authHandler imports and middleware mounts "
                 "across the routes directory.")},
             {"kind": "text", "text": (
                 "Found three main mount points: src/index.ts mounts "
                 "authHandler on /api/v1 and /api/legacy at lines 34–35 "
                 "(app.use); src/routes/websocket-handler.ts calls authHandler "
                 "before the WebSocket upgrade at line 18; and "
                 "src/routes/admin-routes.ts uses authHandler with a "
                 "permission check for admin endpoints at line 9. All three "
                 "share the same middleware contract, so the rate limiter "
                 "integrates uniformly.")}])
    emit(sid=sid, uuid="hg06", parent="hg05", etype="tool_result", when=H(8),
         source=src, cwd=cwd, branch=branch,
         blocks=[tool_result(
             "tu_task",
             ("Found 3 mount points: index.ts (v1 + legacy), "
              "websocket-handler.ts (WS upgrade), admin-routes.ts (admin)."),
             agent_id=agent_hash,
             meta={"total_tokens": 18240, "total_duration_ms": 9120,
                   "total_tool_use_count": 3, "status": "completed"})])
    impl = ("Now I'll create the rate-limiter middleware module that hooks "
            "into the chain after auth — a class that maintains in-memory "
            "token buckets per user and exposes an Express middleware.")
    emit(sid=sid, uuid="hg07", parent="hg06", etype="assistant", when=H(9),
         source=src, cwd=cwd, branch=branch, model=OPUS, msg_id="mh4",
         req_id="rh4", text=impl, blocks=[
             {"kind": "text", "text": impl},
             tool_use("Write", "tu_write",
                      preview="src/middleware/rate-limiter.ts",
                      inp={"file_path": rl_path})])
    emit(sid=sid, uuid="hg08", parent="hg07", etype="tool_result", when=H(10),
         source=src, cwd=cwd, branch=branch,
         blocks=[tool_result("tu_write", _HERO_RATELIMITER_TS)])
    emit(sid=sid, uuid="hg09", parent="hg08", etype="assistant", when=H(11),
         source=src, cwd=cwd, branch=branch, model=OPUS, msg_id="mh5",
         req_id="rh5", blocks=[
             tool_use("Edit", "tu_edit", preview="src/index.ts",
                      inp={"file_path": idx_path})])
    emit(sid=sid, uuid="hg10", parent="hg09", etype="tool_result", when=H(12),
         source=src, cwd=cwd, branch=branch,
         blocks=[tool_result("tu_edit", _HERO_EDIT_DIFF)])
    emit(sid=sid, uuid="hg11", parent="hg10", etype="assistant", when=H(13),
         source=src, cwd=cwd, branch=branch, model=OPUS, msg_id="mh6",
         req_id="rh6", blocks=[
             tool_use("Bash", "tu_bash",
                      preview="npm test -- rate-limiter",
                      inp={"command": "npm test -- --testPathPattern=rate-limiter"})])
    emit(sid=sid, uuid="hg12", parent="hg11", etype="tool_result", when=H(14),
         source=src, cwd=cwd, branch=branch,
         blocks=[tool_result("tu_bash", _HERO_TEST_OUTPUT)])
    summary = ("The token-bucket rate limiter is live in the api-gateway "
               "middleware. I added a RateLimiter class with tier-aware "
               "capacity and refill rates (free 100/min, pro 500/min, "
               "enterprise 5000/min), wired it into the middleware chain after "
               "auth in src/index.ts, and all 7 unit tests pass. For "
               "distributed deployments the in-memory store should move to "
               "Redis so limits hold across gateway instances.")
    emit(sid=sid, uuid="hg13", parent="hg12", etype="assistant", when=H(15),
         source=src, cwd=cwd, branch=branch, model=OPUS, msg_id="mh7",
         req_id="rh7", text=summary,
         blocks=[{"kind": "text", "text": summary}])

    # Cost for the 7 Opus main turns + the Haiku subagent turn.
    for i, (mid, rid) in enumerate(
            [("mh1", "rh1"), ("mh2", "rh2"), ("mh3", "rh3"), ("mh4", "rh4"),
             ("mh5", "rh5"), ("mh6", "rh6"), ("mh7", "rh7")]):
        cost(src, mid, rid, OPUS, H(1 + 2 * i),
             inp=3000, out=1100, cc=1800, cr=14000)
    cost(agent_src, "ms1", "rs1", HAIKU, H(7), inp=2200, out=620, cr=9000)

    # ===== FILLER SESSIONS (populate the rail) ============================
    def filler(proj, sess_branch, day_offset, model, turns):
        s = f"sess-{proj}-00"
        sp = f"{home}/.claude/projects/-{proj}/sess-{proj}-00.jsonl"
        cw = f"/Users/dev/code/{proj}"
        base = as_of - dt.timedelta(days=day_offset, minutes=20)
        prev = None
        for i, (uid, etype, who_model, text) in enumerate(turns):
            when = base + dt.timedelta(minutes=i)
            emit(sid=s, uuid=uid, parent=prev, etype=etype,
                 when=when, source=sp, cwd=cw,
                 branch=sess_branch, text=text, model=who_model,
                 msg_id=(f"{uid}m" if etype == "assistant" else None),
                 req_id=(f"{uid}r" if etype == "assistant" else None),
                 blocks=([{"kind": "text", "text": text}]
                         if etype == "assistant" else None))
            if etype == "assistant":
                cost(sp, f"{uid}m", f"{uid}r", who_model, when,
                     inp=1600, out=520, cc=700, cr=6000)
            prev = uid

    filler("web-app", "main", 1, FABLE, [
        ("f1h1", "human", None,
         "The modal dialog loses focus trapping when the background content "
         "updates — users can tab outside the modal and interact with "
         "elements beneath it. How do I keep focus trapped inside the modal?"),
        ("f1a1", "assistant", FABLE,
         "React modals need explicit focus trapping to prevent tab-escape. Use "
         "`react-focus-lock`, or implement a custom trap with `useEffect` that "
         "moves focus into the modal container on mount and re-initializes "
         "when the background updates. Add `aria-modal='true'` and "
         "`role='dialog'` so assistive tech restricts context, and return "
         "focus to the trigger element on close."),
        ("f1h2", "human", None,
         "Should I use a library or build it myself?"),
        ("f1a2", "assistant", FABLE,
         "For production, use `react-focus-lock` or `react-aria`'s Dialog hook "
         "— they handle nested modals, focus restoration on close, and the "
         "accessibility edge cases hand-rolled traps miss. A custom `useEffect` "
         "is fine for a simple solo modal, but you'll keep patching it."),
    ])
    filler("data-pipeline", "main", 2, OPUS, [
        ("f2h1", "human", None,
         "We're adding a new required field to our event schema, but in-flight "
         "batches don't have it. How do we migrate without breaking "
         "downstream consumers?"),
        ("f2a1", "assistant", OPUS,
         "Add the field as optional first (nullable or with a default) and "
         "deploy that, so existing batches pass through unchanged. Then run a "
         "backfill that reprocesses old events to populate it. Once everything "
         "is backfilled, make the field required. Keep consumers tolerant of "
         "both shapes during the window, behind a feature flag so you can roll "
         "back."),
        ("f2h2", "human", None,
         "How do we retry failed batches without re-processing the whole "
         "pipeline?"),
        ("f2a2", "assistant", OPUS,
         "Keep a checkpoint table with batch ID, last-completed stage, and "
         "timestamp. On failure, push the batch ID + failed stage to a retry "
         "queue; the worker resumes from that checkpoint instead of the start. "
         "You only re-process the stage that failed, not the whole lineage."),
    ])
    filler("mobile-client", "main", 3, SONNET, [
        ("f3h1", "human", None,
         "Users on unreliable networks get stuck with stale data and no idea "
         "whether a sync succeeded. How do I build reliable offline sync with "
         "clear feedback?"),
        ("f3a1", "assistant", SONNET,
         "Use a local SQLite (or Realm) store as the source of truth while "
         "offline, and sync to the backend when connectivity returns. A "
         "`NetworkInfo` listener detects reconnects and triggers a sync worker "
         "that batches pending mutations. Surface a status badge — synced / "
         "syncing / failed with a retry — and store the last-sync timestamp "
         "so you fetch only deltas on reconnect, not the full dataset."),
    ])


def _seed_weekly_cost_snapshot(
    conn: sqlite3.Connection,
    *,
    week_start_date: str,
    week_end_date: str,
    week_start_at: str,
    week_end_at: str,
    cost_usd: float,
    captured_at_utc: str,
) -> None:
    """No helper exists in _fixture_builders.py for this table; inline raw SQL.

    Schema source: production `INSERT INTO weekly_cost_snapshots` in bin/cctally.
    Required columns: captured_at_utc, week_start_date, week_end_date,
    week_start_at, week_end_at, cost_usd. `mode` and `project` default per
    schema; range_start_iso/range_end_iso default to week boundaries to mirror
    a `range-cost`-style snapshot.
    """
    conn.execute(
        """INSERT INTO weekly_cost_snapshots
           (captured_at_utc, week_start_date, week_end_date,
            week_start_at, week_end_at,
            range_start_iso, range_end_iso,
            cost_usd, source, mode, project)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            captured_at_utc,
            week_start_date,
            week_end_date,
            week_start_at,
            week_end_at,
            week_start_at,
            week_end_at,
            cost_usd,
            "build-readme-fixtures",
            "auto",
            None,
        ),
    )


def _seed_percent_milestone(
    conn: sqlite3.Connection,
    *,
    week_start_date: str,
    week_end_date: str,
    week_start_at: str,
    week_end_at: str,
    percent_threshold: int,
    captured_at_utc: str,
    cumulative_cost_usd: float,
    marginal_cost_usd: float,
    five_hour_percent_at_crossing: Optional[float] = None,
) -> None:
    """Schema source: production `INSERT OR IGNORE INTO percent_milestones`
    in bin/cctally:9614. Required NOT NULL columns: captured_at_utc,
    week_start_date, week_end_date, percent_threshold, cumulative_cost_usd,
    usage_snapshot_id, cost_snapshot_id. usage_snapshot_id / cost_snapshot_id
    are arbitrary integers in the fixture (no FK enforcement)."""
    conn.execute(
        """INSERT OR IGNORE INTO percent_milestones
           (captured_at_utc, week_start_date, week_end_date,
            week_start_at, week_end_at,
            percent_threshold,
            cumulative_cost_usd, marginal_cost_usd,
            usage_snapshot_id, cost_snapshot_id,
            five_hour_percent_at_crossing)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            captured_at_utc,
            week_start_date,
            week_end_date,
            week_start_at,
            week_end_at,
            percent_threshold,
            cumulative_cost_usd,
            marginal_cost_usd,
            0,  # usage_snapshot_id — fixture-arbitrary; no FK
            0,  # cost_snapshot_id — fixture-arbitrary; no FK
            five_hour_percent_at_crossing,
        ),
    )


def _seed_five_hour_block(
    conn: sqlite3.Connection,
    *,
    five_hour_window_key: int,
    five_hour_resets_at: str,
    block_start_at: str,
    first_observed_at_utc: str,
    last_observed_at_utc: str,
    final_five_hour_percent: float,
    seven_day_pct_at_block_start: float,
    seven_day_pct_at_block_end: float,
    total_input_tokens: int,
    total_output_tokens: int,
    total_cache_create_tokens: int,
    total_cache_read_tokens: int,
    total_cost_usd: float,
    is_closed: int,
    created_at_utc: str,
    last_updated_at_utc: str,
    crossed_seven_day_reset: int = 0,
) -> None:
    """Schema source: production `INSERT INTO five_hour_blocks`
    in bin/cctally:10076."""
    conn.execute(
        """INSERT INTO five_hour_blocks
           (five_hour_window_key, five_hour_resets_at, block_start_at,
            first_observed_at_utc, last_observed_at_utc,
            final_five_hour_percent,
            seven_day_pct_at_block_start, seven_day_pct_at_block_end,
            crossed_seven_day_reset,
            total_input_tokens, total_output_tokens,
            total_cache_create_tokens, total_cache_read_tokens,
            total_cost_usd,
            is_closed, created_at_utc, last_updated_at_utc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            five_hour_window_key,
            five_hour_resets_at,
            block_start_at,
            first_observed_at_utc,
            last_observed_at_utc,
            final_five_hour_percent,
            seven_day_pct_at_block_start,
            seven_day_pct_at_block_end,
            crossed_seven_day_reset,
            total_input_tokens,
            total_output_tokens,
            total_cache_create_tokens,
            total_cache_read_tokens,
            total_cost_usd,
            is_closed,
            created_at_utc,
            last_updated_at_utc,
        ),
    )


def _populate_weeks(
    stats_conn: sqlite3.Connection,
    *,
    as_of: dt.datetime,
    open_block_window_key: int,
    open_block_final_pct: float,
    open_block_resets_at_iso: str,
) -> None:
    """Seed 8 weekly snapshots ending at as_of's containing week.

    Narrative arc: the user starts at a higher $/1% (~$0.65, less
    efficient), gradually improves through the middle weeks (~$0.43–$0.50),
    then regresses slightly in the most recent closed week and the
    in-progress current week. The visible variance in $/1% is the
    storytelling spine of the Trend chart screenshot — without it, the
    sparkline / chart goes flat and the modal's `median $0.59` label
    collides with the chart line.

    Current-week (i=7) `weekly_percent` is INTENTIONALLY lower than the
    immediately-prior closed week (W-1, i=6) because the current week is
    in progress. Combined with the as_of-aligned-to-Thursday change in
    `build()`, this gives the forecast a clearly-WARN ~103% projection
    that fits within the modal's right edge (the prior 59%/Tuesday combo
    yielded a 261% projection that clipped the pill).

    | i | label   | weekly_pct | cost  | $/1%  |
    |---|---------|-----------:|------:|------:|
    | 0 | W-7     |       38.0 | 24.70 | 0.650 |
    | 1 | W-6     |       41.0 | 25.83 | 0.630 |
    | 2 | W-5     |       44.0 | 25.96 | 0.590 |
    | 3 | W-4     |       47.0 | 24.91 | 0.530 |
    | 4 | W-3     |       50.0 | 25.00 | 0.500 |
    | 5 | W-2     |       53.0 | 22.79 | 0.430 |
    | 6 | W-1     |       56.0 | 25.20 | 0.450 |
    | 7 | current |       53.0 | 28.12 | 0.530 |

    Current-week multiple snapshots: to lift `forecast.confidence` to
    "high" we seed three rows for i=7 (snapshot_count >= 3 AND at least
    one sample with captured_at <= now-24h, see `_assess_forecast_confidence`
    + `has_sample_ge_24h` gate in `_load_forecast_inputs`). The 24h-ago
    sample is at 42.2% so r_recent = (53.0-42.2)/24 = 0.45 %/h, giving a
    ~90% recent-24h projection — clearly below the week-avg ~103%, lands
    in the WARN range without clipping. Latest snapshot (captured at
    as_of) carries the open 5h block's `five_hour_window_key` so the
    dashboard's current_week panel can bind to `five_hour_block`.

    `open_block_window_key` is computed by `_populate_blocks` and threaded
    in here so the latest snapshot's `five_hour_window_key` matches the
    open block's key 1:1 (per the CLAUDE.md gotcha).
    """
    week_start = (as_of - dt.timedelta(days=as_of.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    # round() to keep IEEE-754 trail noise out of the snapshot values
    # (e.g. 2.4000000000000004 → 2.4) so deterministic-dump tests stay
    # stable across Python releases.
    weekly_series = [
        (38.0, 24.70),
        (41.0, 25.83),
        (44.0, 25.96),
        (47.0, 24.91),
        (50.0, 25.00),
        (53.0, 22.79),
        (56.0, 25.20),
        (53.0, 28.12),  # current (in-progress) week — matches live sum
                        # of seeded session_entries so cli-report.svg's cost
                        # column and cli-forecast.svg's spent_usd line agree.
    ]
    for i, (weekly_pct, cost) in enumerate(weekly_series):
        offset = 7 - i  # i=0 = oldest, i=7 = current
        wstart = week_start - dt.timedelta(days=7 * offset)
        wend = wstart + dt.timedelta(days=7)
        weekly_pct = round(weekly_pct, 2)
        cost = round(cost, 2)
        if i < 7:
            # Closed weeks: one captured-at-end-of-week snapshot.
            captured = wend - dt.timedelta(seconds=1)
            seed_weekly_usage_snapshot(
                stats_conn,
                captured_at_utc=_iso(captured),
                week_start_date=wstart.strftime("%Y-%m-%d"),
                week_end_date=wend.strftime("%Y-%m-%d"),
                week_start_at=_iso(wstart),
                week_end_at=_iso(wend),
                weekly_percent=weekly_pct,
                five_hour_percent=18.0,
                five_hour_resets_at=_iso(wend),
                payload_json="{}",
                source="build-readme-fixtures",
            )
        else:
            # Current week: 3 snapshots so forecast hits `confidence=high`.
            # The 24h-ago and 12h-ago samples seed `r_recent` for the
            # forecast modal's "Recent 24h" projection. ONLY the latest
            # snapshot (captured_at = as_of) carries `five_hour_window_key`
            # — `_select_current_block_for_envelope` picks the latest row
            # by captured_at_utc DESC, so the prior two stay NULL on that
            # column to avoid stale-block ambiguity.
            # Latest sample (captured_at = as_of) MUST mirror the open
            # block's final_five_hour_percent + five_hour_resets_at —
            # `_tui_build_current_week` reads these scalar fields off the
            # newest snapshot to populate `current_week.five_hour_pct` /
            # `five_hour_resets_in_sec`, which the React Header chip and
            # CurrentWeekPanel render directly. Older samples never reach
            # those readers (only the latest row by `captured_at_utc DESC`
            # does) so they keep the closed-week-default 18.0 / weekly-end
            # placeholders without affecting any rendered surface.
            current_week_samples = [
                # (captured_at, weekly_pct, five_hour_window_key,
                #  five_hour_pct, five_hour_resets_at)
                (as_of - dt.timedelta(hours=24), 42.2, None,
                    18.0, _iso(wend)),
                (as_of - dt.timedelta(hours=12), 47.6, None,
                    18.0, _iso(wend)),
                (as_of,                          53.0, open_block_window_key,
                    open_block_final_pct, open_block_resets_at_iso),
            ]
            for (captured, sample_pct, window_key,
                 fh_pct, fh_resets_at) in current_week_samples:
                seed_weekly_usage_snapshot(
                    stats_conn,
                    captured_at_utc=_iso(captured),
                    week_start_date=wstart.strftime("%Y-%m-%d"),
                    week_end_date=wend.strftime("%Y-%m-%d"),
                    week_start_at=_iso(wstart),
                    week_end_at=_iso(wend),
                    weekly_percent=round(sample_pct, 2),
                    five_hour_percent=fh_pct,
                    five_hour_resets_at=fh_resets_at,
                    five_hour_window_key=window_key,
                    payload_json="{}",
                    source="build-readme-fixtures",
                )
            captured = as_of  # for the cost snapshot below
        _seed_weekly_cost_snapshot(
            stats_conn,
            week_start_date=wstart.strftime("%Y-%m-%d"),
            week_end_date=wend.strftime("%Y-%m-%d"),
            week_start_at=_iso(wstart),
            week_end_at=_iso(wend),
            cost_usd=cost,
            captured_at_utc=_iso(captured),
        )


def _populate_milestones(
    stats_conn: sqlite3.Connection, *, as_of: dt.datetime
) -> None:
    """Seed percent_milestones for the current week so percent-breakdown
    and the TUI's milestone widget have crossings to display.
    """
    week_start = (as_of - dt.timedelta(days=as_of.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    week_end = week_start + dt.timedelta(days=7)
    wstart_str = week_start.strftime("%Y-%m-%d")
    wend_str = week_end.strftime("%Y-%m-%d")
    wstart_iso = _iso(week_start)
    wend_iso = _iso(week_end)
    # Crossings span Monday 02:00 → Wednesday 06:00, fitting within the
    # current fixture as_of (Thursday 14:00 UTC, see `build()`). Capped at
    # 50% — the current week's weekly_pct is 53.0, which has crossed 50
    # but not yet 60. Emitting 53/59 thresholds was an artifact of the
    # prior 59% current-week target.
    crossings = [
        (10, dt.timedelta(days=0, hours=2),  4.10, 4.10, 6.0),
        (20, dt.timedelta(days=0, hours=8),  8.95, 4.85, 11.0),
        (30, dt.timedelta(days=0, hours=14), 13.40, 4.45, 14.5),
        (40, dt.timedelta(days=0, hours=20), 17.80, 4.40, 16.0),
        (50, dt.timedelta(days=1, hours=6),  22.05, 4.25, 18.5),
    ]
    for pct, delta, cumul, marginal, fh_pct in crossings:
        crossed_at = week_start + delta
        if crossed_at > as_of:
            continue
        _seed_percent_milestone(
            stats_conn,
            week_start_date=wstart_str,
            week_end_date=wend_str,
            week_start_at=wstart_iso,
            week_end_at=wend_iso,
            percent_threshold=pct,
            captured_at_utc=_iso(crossed_at),
            cumulative_cost_usd=cumul,
            marginal_cost_usd=marginal,
            five_hour_percent_at_crossing=fh_pct,
        )


def _populate_session_entries(
    cache_conn: sqlite3.Connection, *, as_of: dt.datetime
) -> None:
    """Seed session_entries + session_files spread across the past 30 days.

    The dashboard's Daily heatmap renders 30 days; previously the builder
    only seeded entries inside `[week_start, as_of]`, leaving the heatmap
    mostly empty. Now we walk DAYS backward from `as_of` for 30 days and
    deterministically place 3-6 entries per day, cycling through (project,
    session, model) combinations so:
      - each of the 4 projects appears ≥5 times (regression-tested)
      - each day has ≥1 entry (heatmap fills)
      - models rotate Sonnet/Opus/Haiku for a realistic mix in the model
        breakdowns shown in cli-five-hour-blocks.svg

    Each entry is anchored to a session JSONL path under the fake home's
    .claude/projects/<project> tree (the file doesn't have to exist on
    disk for cache-only commands; session_files row is enough).
    """
    home = REPO_ROOT / "tests/fixtures/readme/home"

    # Pre-create 3 sessions per project so reads see a realistic
    # session_files set (independent of which sessions the 30-day walk
    # ends up touching).
    session_paths: dict[tuple[str, int], str] = {}
    for proj in PROJECTS:
        # The session cwd / project_path MUST resolve outside any git repo,
        # else `_resolve_project_key` walks up to the cctally-dev checkout's
        # .git and collapses all four projects into a single "cctally-dev"
        # bucket — which both looks broken AND leaks the private repo name
        # into the public Projects-panel screenshot. A synthetic absolute
        # path under a non-existent /Users/dev tree resolves to its own
        # basename (web-app / api-gateway / …) with git_root=None.
        cwd = f"/Users/dev/code/{proj}"
        for sess in range(3):
            session_id = f"sess-{proj}-{sess:02d}"
            jsonl_path = f"{home}/.claude/projects/-{proj}/{session_id}.jsonl"
            seed_session_file(
                cache_conn,
                path=jsonl_path,
                session_id=session_id,
                project_path=cwd,
                # size_bytes=0: these synthetic JSONL paths never exist on
                # disk, so a non-zero size makes sync_cache's orphan scan emit
                # "[cache] N tracked file(s) no longer on disk" on every run —
                # which freeze then bakes into the CLI SVGs. The scan only
                # flags non-zero-size tracked paths, so 0 silences it at the
                # source (the walk never finds these absent files to ingest
                # anyway; session_entries are seeded directly).
                size_bytes=0,
                last_byte_offset=0,
            )
            session_paths[(proj, sess)] = jsonl_path

    line_offset = 0
    # days_ago = 0 is the day containing `as_of`; 29 is ~30 days back.
    for days_ago in range(30):
        day_anchor = as_of - dt.timedelta(days=days_ago)
        # Anchor each day at 09:00 UTC so we can fan out 3-6 entries
        # across 09:00..18:00 and stay safely before `as_of` even on the
        # `days_ago == 0` slice (as_of is 14:00 UTC; entries at 09..13
        # all fit). On past days, every entry slot is in the past anyway.
        day_start = day_anchor.replace(
            hour=9, minute=0, second=0, microsecond=0,
        )
        entries_this_day = 3 + (days_ago % 4)  # cycles 3,4,5,6
        for k in range(entries_this_day):
            proj_idx = (days_ago + k) % len(PROJECTS)
            proj = PROJECTS[proj_idx]
            sess_idx = ((days_ago // 7) + k) % 3
            jsonl_path = session_paths[(proj, sess_idx)]
            # Established three rotate evenly (original cost-tuned mix);
            # Fable — the newest, ~2x-Opus premium model — substitutes one
            # entry in nine as a deliberate minority. It stays visible in
            # every model breakdown without implying a daily workhorse, and
            # (with the lighter token profile below) keeps current-week
            # spend on its gentle trend.
            model = BASE_CYCLE[(days_ago + k) % len(BASE_CYCLE)]
            if (days_ago * 4 + k) % 9 == 4:
                model = FABLE_MODEL
            # Space entries across the working day. Step is 90 min so
            # `entries_this_day=6` lands the last one at 16:30, well
            # before any plausible `as_of`.
            ts = day_start + dt.timedelta(minutes=90 * k)
            if ts > as_of:
                continue
            if model == FABLE_MODEL:
                # Fable prices ~2x Opus. Give it a lighter token profile
                # than the big three (roughly half the cache-read context)
                # so it reads as a premium model used for the harder,
                # higher-output calls — and size it so the few Fable entries
                # land the current-week live spend back on the tuned trend
                # ($28.1x), matching the seeded weekly_cost_snapshots the
                # report/dashboard Trend panel renders.
                inp = 32_000 + 5_000 * k + 2_500 * proj_idx
                out = 25_000 + 2_500 * k + 1_000 * (days_ago % 5)
                cc = 10_000 if k % 3 == 0 else 0
                cr = 105_000 + 10_000 * k + 14_000 * (days_ago % 3)
            else:
                # Token counts sized so the per-week live cost adds up
                # to ~$25-30 (matches the trend's $/1% × ~53% target).
                # Spread variance so per-row dashboard numbers don't
                # all look identical.
                # Coefficients are 10x the per-row variance you'd expect
                # for ~1-2k LoC operations; sized so summed live cost over
                # the current week (May 4 → as_of Thursday 14:00 UTC)
                # lands at ~$28 — matches the trend's $28.12 row, the
                # forecast's "Used 53.0% $28.x" line, and the dashboard's
                # `current_week.spent_usd`. Pre-scale, the same week
                # summed to ~$2.81, contradicting the trend's $/1% column.
                inp = 120_000 + 20_000 * k + 8_000 * proj_idx
                out = 48_000 + 6_000 * k + 2_000 * (days_ago % 5)
                cc = 40_000 if k % 3 == 0 else 0
                cr = 180_000 + 15_000 * k + 25_000 * (days_ago % 3)
            seed_session_entry(
                cache_conn,
                source_path=jsonl_path,
                line_offset=line_offset,
                timestamp_utc=_iso(ts),
                model=model,
                input_tokens=inp,
                output_tokens=out,
                cache_create=cc,
                cache_read=cr,
                msg_id=f"msg_{proj}_{sess_idx}_{days_ago}_{k}",
                req_id=f"req_{proj}_{sess_idx}_{days_ago}_{k}",
            )
            line_offset += 1


def _populate_blocks(
    stats_conn: sqlite3.Connection, *, as_of: dt.datetime
) -> tuple[int, float, str]:
    """Seed five_hour_blocks for the current week + 3 prior 5h windows.

    Anchor blocks at 10:00 UTC on each of the past 4 days. Latest block
    (offset 0) stays open (`is_closed=0`); prior blocks are closed.

    Returns ``(window_key, final_pct, resets_at_iso)`` for the OPEN block
    (offset_days=0). Callers (`_populate_weeks`) mirror these onto the
    latest weekly_usage_snapshots row so the snapshot scalars
    (`five_hour_percent`, `five_hour_resets_at`) match the open block —
    `_tui_build_current_week` reads them directly for the React Header
    chip and CurrentWeekPanel display, and `five_hour_window_key` joins
    `_select_current_block_for_envelope` to the same block.

    `seven_day_pct_at_block_start` for the open block is tuned BELOW the
    current-week `used_pct` (53.0) so the panel's delta lands at a
    positive few percent. Closed blocks keep the prior linear scheme.
    """
    base_today = as_of.replace(hour=10, minute=0, second=0, microsecond=0)
    open_block_window_key: Optional[int] = None
    open_block_final_pct: Optional[float] = None
    open_block_resets_at_iso: Optional[str] = None
    for offset_days in (3, 2, 1, 0):
        block_start = base_today - dt.timedelta(days=offset_days)
        if block_start > as_of:
            continue
        block_end = block_start + dt.timedelta(hours=5)
        last_observed = min(block_end - dt.timedelta(minutes=20), as_of)
        first_observed = block_start + dt.timedelta(minutes=2)
        is_closed = 0 if offset_days == 0 else 1
        five_h_pct = 22.0 + 14.0 * (3 - offset_days)
        # Calibrated to roughly match the live per-block recompute sum from
        # session_entries (which `cmd_five_hour_blocks --breakdown=model`
        # surfaces in the second column). Active block is partial — only
        # the entries inside [block_start, as_of] count, vs 3 entries
        # spanning the full 5h window for closed blocks. If the breakdown
        # rows visibly out-sum the parent after a `CLAUDE_MODEL_PRICING`
        # change, retune these constants. (Production cctally recomputes
        # the parent on every `record-usage` tick from the same
        # session_entries; the marketing fixture skips that path because
        # there's no live OAuth flow, so we hand-seed instead.)
        cost = 2.70 if offset_days == 0 else 4.50 + 0.10 * (3 - offset_days)
        if offset_days == 0:
            # Open block: anchor BELOW current 53.0 so `used_pct -
            # seven_day_pct_at_block_start` is a small positive delta
            # (+3.0pp). Anthropic's OAuth API only returns INTEGER
            # weekly percentages — `seven_day_pct_at_block_start` is
            # populated from `weekly_usage_snapshots.weekly_percent` at
            # block-start time, so any non-integer value here is
            # narratively impossible (would render "+2.5pp this block"
            # and break the screenshot's credibility).
            seven_day_start = 50.0
            seven_day_end = 53.0
        else:
            seven_day_start = 40.0 + 5.0 * (3 - offset_days)
            seven_day_end = seven_day_start + 2.0
        # Canonical 5h window key: epoch seconds floored to 10 minutes.
        # Mirrors _canonical_5h_window_key (bin/cctally) — fixtures should
        # use the same shape so harnesses join cleanly.
        window_key = int(block_start.timestamp() // 600 * 600)
        if offset_days == 0:
            open_block_window_key = window_key
            open_block_final_pct = five_h_pct
            open_block_resets_at_iso = _iso(block_end)
        _seed_five_hour_block(
            stats_conn,
            five_hour_window_key=window_key,
            five_hour_resets_at=_iso(block_end),
            block_start_at=_iso(block_start),
            first_observed_at_utc=_iso(first_observed),
            last_observed_at_utc=_iso(last_observed),
            final_five_hour_percent=five_h_pct,
            seven_day_pct_at_block_start=seven_day_start,
            seven_day_pct_at_block_end=seven_day_end,
            total_input_tokens=42_000 + 8_000 * (3 - offset_days),
            total_output_tokens=15_000 + 3_000 * (3 - offset_days),
            total_cache_create_tokens=1_200,
            total_cache_read_tokens=22_000,
            total_cost_usd=cost,
            is_closed=is_closed,
            created_at_utc=_iso(first_observed),
            last_updated_at_utc=_iso(last_observed),
        )
    if (
        open_block_window_key is None
        or open_block_final_pct is None
        or open_block_resets_at_iso is None
    ):
        # Should not happen given the (3, 2, 1, 0) sweep + fixed Thursday
        # 14:00 anchor, but fail loud rather than silently emit a fixture
        # with NULL fields on the latest snapshot.
        raise RuntimeError(
            "open block (offset_days=0) was not seeded; current 5h block "
            "envelope binding will fail"
        )
    return (
        open_block_window_key,
        open_block_final_pct,
        open_block_resets_at_iso,
    )


MARKETING_DISPLAY_TZ = "America/Los_Angeles"

# Deterministic 32-char hex placeholder for `collector.token`. cctally's
# real `_default_config_data()` uses `secrets.token_hex(16)`, but the
# marketing fixture must round-trip identically across builds (the
# screenshot pipeline is byte-stable; the determinism harness in
# `tests/test_build_readme_fixtures.py::test_deterministic_for_fixed_as_of`
# verifies SQLite dumps but config.json should also stay byte-identical
# across runs to make harness diffs easy to read). The value is a fixed
# constant — never used to authenticate against a real collector since
# `bin/cctally cache-sync` against this fixture does not POST.
_MARKETING_FIXTURE_COLLECTOR_TOKEN = "0123456789abcdef0123456789abcdef"


def _write_marketing_config(app_dir: Path) -> None:
    """Write `<app_dir>/config.json` pinning `display.tz` to LA.

    Mirrors the structure of cctally's `_default_config_data()` plus the
    `display.tz` block users would set via `cctally config set
    display.tz America/Los_Angeles`. A fresh fake-home has no prior
    config; we lay one down so the dashboard + CLI render dates in LA
    time independent of host TZ.
    """
    app_dir.mkdir(parents=True, exist_ok=True)
    config_path = app_dir / "config.json"
    data = {
        "collector": {
            "host": "127.0.0.1",
            "port": 17321,
            "token": _MARKETING_FIXTURE_COLLECTOR_TOKEN,
            "week_start": "monday",
        },
        "display": {
            "tz": MARKETING_DISPLAY_TZ,
        },
    }
    config_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_tui_snapshot(path: Path, *, as_of: dt.datetime) -> None:
    """Write a Python module exporting SNAPSHOT (DataSnapshot) for the TUI shot.

    The exact DataSnapshot type lives in bin/cctally; the snapshot module
    is loaded by `tui --render-once --snapshot-module PATH` per the dev
    contract. We keep the snapshot body minimal and let the loader's
    RUNTIME_OVERRIDES path tweak modal state if a future variant needs it.
    """
    body = f'''"""Auto-generated by bin/build-readme-fixtures.py — do not hand-edit.

Loaded by `cctally tui --render-once --snapshot-module …` per the dev path.
Exports SNAPSHOT (required) and may export RUNTIME_OVERRIDES (optional).
"""
from __future__ import annotations

# DataSnapshot is defined inside bin/cctally; we lazy-import to keep this
# module loadable by tests that don't have the binary on PYTHONPATH.
import importlib.util as _ilu
import importlib.machinery as _ilm
import sys as _sys
from pathlib import Path as _Path

# Walk up from this snapshot file to find bin/cctally. The earlier
# `parents[3]` form only worked when the snapshot lived at the default
# committed path (tests/fixtures/readme/tui_snapshot.py). For a custom
# `--tui-snapshot` path, parents[3] resolves outside the repo and the
# import fails before the tui renderer runs.
_HERE = _Path(__file__).resolve().parent
_BIN = None
for _p in [_HERE, *_HERE.parents]:
    _candidate = _p / "bin" / "cctally"
    if _candidate.is_file():
        _BIN = _candidate
        break
if _BIN is None:
    raise RuntimeError(
        "could not locate bin/cctally walking up from "
        f"{{_HERE}}; place the snapshot inside the cctally repo"
    )
_spec = _ilu.spec_from_loader(
    "_cctally_for_tui_snapshot",
    _ilm.SourceFileLoader("_cctally_for_tui_snapshot", str(_BIN)),
)
_mod = _ilu.module_from_spec(_spec)
_sys.modules.setdefault("_cctally_for_tui_snapshot", _mod)
_spec.loader.exec_module(_mod)

DataSnapshot = _mod.DataSnapshot

SNAPSHOT = DataSnapshot.synthesize_for_marketing(as_of_iso="{_iso(as_of)}")

RUNTIME_OVERRIDES = {{}}
'''
    path.write_text(body)


def build(
    *,
    out_dir: Path,
    as_of_str: str,
    tui_snapshot_path: Optional[Path] = None,
) -> None:
    """Top-level builder. Idempotent — overwrites existing DBs.

    `as_of_str` is interpreted as the calendar week to render; the actual
    "now" used for fixture generation is THURSDAY 14:00 UTC of the week
    containing that date. This makes the forecast projection land in the
    95-105% range (used_pct=53 / elapsed_fraction≈0.512 → ~103.5%) rather
    than the prior Tuesday-anchored ~261% projection that clipped the
    WARN modal's projection pill at the right edge. Callers pinning a
    specific date (e.g. tests using `2026-05-05` Tuesday) silently land
    on the same week's Thursday — `2026-05-07` in that example.
    """
    parsed = dt.datetime.strptime(as_of_str, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc,
    )
    # Shift to the THURSDAY of the containing week.
    # weekday(): Mon=0..Sun=6; Thursday=3.
    days_to_thursday = 3 - parsed.weekday()
    as_of = (parsed + dt.timedelta(days=days_to_thursday)).replace(
        hour=14, minute=0, second=0, microsecond=0,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    stats_path = out_dir / "stats.db"
    cache_path = out_dir / "cache.db"
    if stats_path.exists():
        stats_path.unlink()
    if cache_path.exists():
        cache_path.unlink()

    create_stats_db(stats_path)
    create_cache_db(cache_path)

    with sqlite3.connect(stats_path) as stats_conn:
        # WAL is set by create_stats_db() but a fresh connect()
        # re-asserts the pragma cheaply and matches production posture.
        stats_conn.execute("PRAGMA journal_mode=WAL")
        stats_conn.execute("PRAGMA foreign_keys = OFF")
        # Seed blocks FIRST so we know the open block's canonical
        # five_hour_window_key, then thread it into _populate_weeks so
        # the latest weekly_usage_snapshots row carries the same key.
        # Without that mirror, `_select_current_block_for_envelope`
        # returns None and the dashboard's current-week panel renders
        # the legacy single-big-number layout.
        (
            open_block_window_key,
            open_block_final_pct,
            open_block_resets_at_iso,
        ) = _populate_blocks(stats_conn, as_of=as_of)
        _populate_weeks(
            stats_conn,
            as_of=as_of,
            open_block_window_key=open_block_window_key,
            open_block_final_pct=open_block_final_pct,
            open_block_resets_at_iso=open_block_resets_at_iso,
        )
        _populate_milestones(stats_conn, as_of=as_of)
        # Ship as a fully-migrated user so a read command's sync_cache walk
        # can't trip the #93 upgrade-gate and recompute the seeded
        # weekly_cost_snapshots / five_hour_blocks / percent_milestones to $0
        # (the pre-walk trend weeks have no session_entries to recompute
        # from). This is the same guard the dashboard / share / conversation
        # render-fixture builders apply; without it `cctally report` zeroes
        # the three oldest trend rows in cli-report.svg.
        stamp_all_stats_migrations_applied(stats_conn)
        stats_conn.commit()

    with sqlite3.connect(cache_path) as cache_conn:
        cache_conn.execute("PRAGMA journal_mode=WAL")
        _populate_session_entries(cache_conn, as_of=as_of)
        _populate_conversations(cache_conn, as_of=as_of)
        _recompute_conversation_sessions(cache_conn)
        cache_conn.commit()

    # config.json: pin display.tz so dashboard + CLI render dates in LA
    # time regardless of host TZ. Without this, screenshots inherit the
    # maintainer's local zone (IDT on the host that built the prior
    # round) and dates drift visibly between machines. Writing directly
    # is safe — nothing else holds the file in this build path.
    # `out_dir` is `<home>/.local/share/cctally`; production reads
    # `config.json` from the same directory.
    _write_marketing_config(out_dir)

    snap_path = tui_snapshot_path or DEFAULT_TUI_SNAPSHOT
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    _write_tui_snapshot(snap_path, as_of=as_of)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--as-of",
        default=None,
        help="YYYY-MM-DD anchor for relative dates (default: today UTC)",
    )
    p.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR),
        help="Output directory for stats.db / cache.db (default: %(default)s)",
    )
    p.add_argument(
        "--tui-snapshot",
        default=str(DEFAULT_TUI_SNAPSHOT),
        help="Path for the TUI snapshot module (default: %(default)s)",
    )
    args = p.parse_args()
    as_of = args.as_of or DEFAULT_AS_OF_FN()
    build(
        out_dir=Path(args.out),
        as_of_str=as_of,
        tui_snapshot_path=Path(args.tui_snapshot),
    )
    print(f"wrote fixture: out={args.out} as_of={as_of}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
