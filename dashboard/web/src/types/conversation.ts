// Conversation viewer API types (spec §4 / §5). Bound field-for-field to
// the shipped backend in bin/_lib_conversation_query.py +
// bin/_cctally_dashboard.py. These mirror the three GET routes
// (/api/conversations, /api/conversation/<id>, /api/conversation/search).

// Prompt-cache-failure marker (cache-failure-markers spec §2). Stamped by the
// kernel's `_stamp_cache_failures` onto an assistant turn that re-created the
// bulk of its cached prefix instead of reading it. ABSENT on healthy turns
// (matching the `tokens?` "absent, not zero" convention) — never zero-filled.
//   tokens_recreated — the previously-cached prefix that had to be re-created:
//                      `min(cc, max(0, rm - cr))` (NOT the raw cache_creation).
//   prev_cached      — the running-max cache_read before this turn (rm).
//   est_wasted_usd   — the marginal write-vs-read cost on the lost prefix
//                      (display-only estimate; never summed into a cost figure).
export interface CacheFailure {
  tokens_recreated: number;
  prev_cached: number;
  est_wasted_usd: number;
}

export type ConversationItem =
  | {
      // assistant turn
      kind: 'assistant';
      anchor: { session_id: string; uuid: string; id: number }; // uuid = prose-bearing fragment
      member_uuids: string[]; // every fragment uuid folded into this turn
      ts: string;
      text: string; // joined prose
      blocks: ConversationBlock[];
      model: string | null;
      is_sidechain: boolean;
      subagent_key: string | null; // agent-file hash; null for the main session
      parent_uuid: string | null;  // raw parent uuid (for cross-file nesting)
      cost_usd: number; // the TURN's cost, counted ONCE (0.0 for null msg_id)
      // #177 S1 backend / S5 client adoption (Codex F7) — per-turn token usage,
      // stamped when the turn key has a session_entries row. Absent (NOT
      // zero-filled) otherwise; the §6 footer reads it.
      tokens?: TokenUsage;
      // cache-failure-markers spec §2/§3 — present only on a flagged assistant
      // turn; absent on healthy ones. The reader chip reads it.
      cache_failure?: CacheFailure;
    }
  | {
      // human or tool_result (also: assistant-with-null-msg_id, which carries model + cost_usd: 0)
      kind: 'human' | 'tool_result' | 'assistant';
      anchor: { session_id: string; uuid: string; id: number };
      member_uuids: string[]; // always [uuid]
      ts: string;
      text: string; // "" for tool_result rows
      blocks: ConversationBlock[];
      is_sidechain: boolean;
      subagent_key: string | null;
      parent_uuid: string | null;
      model?: string | null; // present only on the null-msg_id assistant case
      cost_usd?: number; // present (0.0) only on the null-msg_id assistant case
      // #188 — a slash-command invocation promoted to a "You" turn (text=args)
      // carries the command name for a compact badge; the kernel derives it from
      // the raw <command-name> block, NOT the scalar text (which holds the args).
      // Absent/null on an ordinary human turn. Consumers tolerate the missing key.
      command_name?: string | null;
      // cache-failure-markers spec §2 — declared on this arm too so
      // `item.cache_failure` type-checks after a `kind === 'assistant'` narrow
      // resolves to the null-msg_id assistant fallback. Absent on human /
      // tool_result rows in practice.
      cache_failure?: CacheFailure;
    }
  | {
      // Injected harness content (isMeta) the user did NOT type — rendered as a
      // collapsed disclosure, never a "You" prompt. `meta_kind` picks the chrome:
      // 'skill' (skill body, with skill_name) / 'command' (slash-command plumbing,
      // raw <pre>) / 'context' (git-context, "Continue…", placeholders, "## Task").
      // `text` is the rendered body (the kernel populates it from blocks; the DB
      // text column stays '' so meta is not FTS-indexed).
      kind: 'meta';
      anchor: { session_id: string; uuid: string; id: number };
      member_uuids: string[];
      ts: string;
      text: string;
      blocks: ConversationBlock[];
      is_sidechain: boolean;
      subagent_key: string | null;
      parent_uuid: string | null;
      meta_kind: 'skill' | 'command' | 'context' | 'compaction' | 'notification';
      skill_name: string | null;
    };

// #177 S1 backend / S5 client adoption — per-turn token usage, stamped on
// assistant turn items when the turn key has a session_entries row. Absent
// (NOT zero-filled) otherwise. Shared by the detail item and the outline turn.
export interface TokenUsage {
  input: number;
  output: number;
  cache_creation: number;
  cache_read: number;
}

// #177 S5 — GET /api/conversation/<id>/outline (spec §1). `ts` nullable (F6).
export interface OutlineToolRef { name: string | null; is_error: boolean; }
export interface OutlineTurn {
  uuid: string;
  kind: 'assistant' | 'human' | 'tool_result' | 'meta';
  ts: string | null;
  label: string;
  member_uuids: string[];
  subagent_key: string | null;
  parent_uuid: string | null;
  is_sidechain: boolean;
  model?: string;
  tokens?: TokenUsage;
  tools?: OutlineToolRef[];
  thinking?: string[];
  meta_kind?: 'skill' | 'command' | 'context' | 'compaction' | 'notification';
  skill_name?: string | null;
  // cache-failure-markers spec §2/§4 — copied from the assembled item onto its
  // OutlineTurn where `tokens` is copied. Present only on a flagged turn.
  cache_failure?: CacheFailure;
}
// Session-modal cache-rebuilds (2026-06-16 spec §1) — one flagged rebuild turn.
export interface CacheRebuild {
  uuid: string;            // the flagged turn's anchor uuid (jump target)
  subagent_key: string | null;  // null = main session; set = subagent thread
  ts: string | null;       // rebuild turn timestamp (nullable, like OutlineTurn.ts)
  tokens_recreated: number;
  est_wasted_usd: number;  // display-only marginal cost
}
export interface OutlineStats {
  turns: { total: number; human: number; assistant: number; tool_result: number; meta: number };
  tool_counts: Record<string, number>;
  error_count: number;
  models: Record<string, number>;
  duration_seconds: number | null;
  tokens: TokenUsage;
  cost_usd: number;
  // cache-failure-markers spec §2/§4 — session-level aggregate of the flagged
  // turns. PRESENT ONLY when count > 0 (the stats "Cache" row renders only
  // then); absent when no turn was flagged. `est_wasted_usd` is display-only.
  cache_failures?: {
    count: number;
    tokens_recreated: number;
    est_wasted_usd: number;
    rebuilds: CacheRebuild[];   // worst-first (by est_wasted_usd desc)
  };
  // Session cache-value-saved (2026-06-16 spec §1): ALWAYS present (0.0 when no
  // cache reads). Display-only — never a reconciled figure.
  cache_saved_usd: number;
}
export interface ConversationOutline {
  session_id: string;
  subagent_meta?: Record<string, SubagentMeta>;
  stats: OutlineStats;
  turns: OutlineTurn[];
}

// One row of a checklist card (TodoWrite legacy + the live Task* family). The
// shared ChecklistCard renderer normalizes an unknown `status` to 'pending'.
export interface ChecklistTodo {
  content: string;
  status: string;
  activeForm?: string;
}

// #177 S4 — media placeholder carried on tool_result blocks (result.media /
// orphan block media) and, with `index`, on user-content image/document
// blocks. `bytes` is the BASE64 length in the source JSONL (decoded ≈ ×3/4).
// `index` is the ingest-stamped ordinal among media items (the media route's
// address); absent on pre-reingest rows → the figure degrades to the badge.
export interface MediaRef {
  kind: 'image' | 'document';
  media_type: string | null;
  bytes: number;
  index: number;
}

export type ConversationBlock =
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  // 'tool_use' is the id-less degradation fallback ONLY (pre-migration rows the
  // kernel never paired): post-migration the kernel always emits 'tool_call'.
  | { kind: 'tool_use'; name: string | null; input_summary: string }
  // 'tool_call' (#164) — a request paired with its matched result in one unit.
  // Mirrors the kernel's Phase-3 sweep field-for-field
  // (bin/_lib_conversation_query.py): result is the folded tool_result, or null
  // when the request had no matched result (request-only).
  //
  // skill_body/skill_name (skill-content nesting): present ONLY on a Skill
  // tool_call whose injected skill body the kernel folded into the chip
  // (matching the body's source_tool_use_id). When skill_body != null the chip
  // expands to the rich-markdown body itself (no request/result panels) and the
  // kernel clears `result`. Absent on every non-folded tool_call (back-compat;
  // consumers tolerate unknown keys).
  | {
      kind: 'tool_call';
      name: string | null;
      input_summary: string;
      input?: Record<string, unknown> | null;  // #177 S1 — bounded structured input
      input_truncated?: boolean;                // #177 S1
      // #198 — true {add, del} stat computed from the FULL input at ingest, stamped
      // ONLY on truncated edit-family calls (Write/Edit/MultiEdit). The DiffCard
      // header prefers it while truncated-and-not-yet-loaded so the badge shows the
      // document's real line count, not the post-clip count. Absent otherwise
      // (non-truncated cards recount from their live jsdiff hunks; legacy rows).
      edit_stat?: { add: number; del: number };
      preview: string;
      tool_use_id: string | null;
      // #177 S4 — `media` (tool-result media placeholders, render-ready) folds
      // into the result object on owned calls; absent when the result carried
      // no image/document items (and on pre-009-reingest rows).
      result: { text: string; truncated: boolean; full_length?: number | null; is_error: boolean; media?: MediaRef[] } | null;
      answers?: Record<string, string>;         // #177 S2 — {question: chosen label(s)}
      annotations?: Record<string, unknown>;    // #177 S2 — user notes keyed by question
      // #177 S3 — Bash stream split, stamped at the BLOCK level (siblings of
      // `answers`, NOT nested in `result`, which is null on unfolded calls). The
      // query kernel's Phase-3 sweep sets `stderr` only when captured and
      // `interrupted` only when true; both absent on non-Bash + legacy rows.
      stderr?: string | null;                   // #177 S3 — Bash stderr
      interrupted?: boolean;                    // #177 S3 — Bash Ctrl-C
      skill_body?: string;
      skill_name?: string | null;
      // Task* checklist: the running to-do list snapshot at this point in the
      // conversation, stamped by the kernel's _fold_task_runs onto the FIRST
      // tool_call of a TaskCreate/TaskUpdate/TaskList run. Absent on non-Task
      // runs and on legacy rows the fold never reached (consumers tolerate the
      // missing key and degrade to generic chips).
      task_snapshot?: ChecklistTodo[];
      // #177 S4 — folded by the kernel's name-keyed Phase-3 join; absent on
      // old rows (pre-009-reingest) and on every non-web tool. `code_text` is
      // omitted at capture when the HTTP status text was empty.
      web_search?: { query: string; links: { title: string; url: string }[]; links_truncated?: boolean };
      web_fetch?: { code: number; code_text?: string };
    }
  // 'tool_result' BLOCK kind survives ONLY inside a standalone orphan
  // tool_result ITEM (a result the kernel could not fold into a request).
  // #177 S4 — orphan results keep `tool_use_id` + `media` so their screenshots
  // still render (the kernel surfaces media on the standalone result block).
  | { kind: 'tool_result'; text: string; truncated: boolean; is_error: boolean; tool_use_id?: string | null; media?: MediaRef[] }
  // #177 S4 — `index` is the ingest-stamped media ordinal (the uuid-mode route
  // address); absent on pre-reingest rows → the figure degrades to the badge.
  | { kind: 'image'; media_type: string | null; bytes: number; index?: number }
  | { kind: 'document'; media_type: string | null; bytes: number; index?: number }
  | { kind: 'tool_reference'; name: string | null };

export interface ConversationSummary {
  session_id: string;
  title: string; // derived conversation title (first real user line; #165 Q-F1)
  project_label: string;
  git_branch: string | null;
  started_utc: string;
  last_activity_utc: string;
  msg_count: number;
  cost_usd: number;
  models: string[];
}

export interface ConversationsPage {
  conversations: ConversationSummary[];
  // `filter_degraded` (filters spec §1 dual-branch parity) is present ONLY when a
  // project/cost/rebuild filter was requested but the rollup was non-authoritative
  // (the live `GROUP BY` fallback can only filter by date). The rail surfaces it as
  // a muted note; absent on the normal authoritative path.
  page: { next_offset: number | null; has_more: boolean; filter_degraded?: boolean };
}

// Browse-list filters (filters spec §4). Session-only client state — never
// persisted across reload. `datePreset` is a chip-LABEL only ('this-month' /
// 'last-month' / 'last-7d' / 'YYYY-MM'); the concrete `dateFrom`/`dateTo`
// 'YYYY-MM-DD' bounds drive the request. The server resolves naive bounds in
// `display.tz` as a half-open [start_of_day, start_of_next_day) interval.
export interface ConversationFilters {
  dateFrom: string | null;   // 'YYYY-MM-DD'
  dateTo: string | null;     // 'YYYY-MM-DD'
  datePreset: string | null; // 'this-month' | 'last-month' | 'last-7d' | 'YYYY-MM' | null (chip label only)
  projects: string[];
  costMin: number | null;
  costMax: number | null;
  rebuildMin: number | null;
}

export const EMPTY_FILTERS: ConversationFilters = {
  dateFrom: null, dateTo: null, datePreset: null,
  projects: [], costMin: null, costMax: null, rebuildMin: null,
};

// GET /api/conversations/facets — sorted distinct project labels + per-label
// conversation count, for the filter popover's project multi-select.
export interface ConversationFacets {
  projects: { project_label: string; count: number }[];
}

// #166: per-subagent kind + toolUseResult meta, keyed by subagent_key (the same
// agent-file hash the reader groups subagent threads on). Whole-session, present
// on every page (empty case is `{}`). Old transcripts produce no entry for a
// given key → the card falls back to its title-only rendering.
export interface SubagentMeta {
  kind: string;
  description?: string;   // #193 — spawning Task description (server-harvested)
  total_tokens?: number;
  total_duration_ms?: number;
  total_tool_use_count?: number;
  status?: string;
  // §4 1b — cross-file parent linkage (read-time, no migration).
  parent_subagent_key?: string | null;   // null = main session; a hash = parent subagent
  spawn_uuid?: string | null;             // the parent-thread item to render this child after
  spawn_tool_use_id?: string | null;      // exact spawn id (one item may hold several spawns)
  // §4 1c — totals derived from the child's own thread (render a "~" affordance).
  totals_derived?: boolean;
}

export interface ConversationDetail {
  session_id: string;
  title?: string;   // #193 — server-derived (ai-title -> first prompt -> label -> sid)
  project_label: string;
  git_branch: string | null;
  started_utc: string;
  last_activity_utc: string;
  cost_usd: number;
  models: string[];
  items: ConversationItem[];
  page: { next_after: number | null; has_more: boolean };
  subagent_meta?: Record<string, SubagentMeta>;  // keyed by subagent_key (#166)
  // jump-to-latest spec §3 — the conversation's final RENDERED turn (the last
  // grouped/deduped item, not the last raw JSONL row). Constructed explicitly by
  // the server with the request session_id (the assembled item's anchor carries a
  // null session_id, Codex P2 #4). `null` only for a genuinely empty conversation
  // (the Jump-to-latest control hides). Task 4 consumes it.
  last_anchor?: { session_id: string; uuid: string; id: number } | null;
}

// #177 S6 — kind facet for the rail search chips + the find bar. Maps 1:1 to
// the backend `kind` param (all | prompts | assistant | tools | thinking).
export type SearchKind = 'all' | 'prompts' | 'assistant' | 'tools' | 'thinking';

export interface SearchHit {
  session_id: string;
  uuid: string;
  project_label: string;
  title: string; // derived conversation title for the hit's session (#165 Q4)
  ts: string;
  snippet: string;
  cost_usd: number;
  // #177 S6 — non-prose match badges (sorted lowercase; server omits prose).
  // Always present on the wire (defaults to []); optional here for back-compat
  // with fixtures predating the field.
  match_kinds?: ('tool' | 'thinking')[];
}

export interface ConversationSearchResult {
  query: string;
  mode: 'fts' | 'like';
  hits: SearchHit[];
  total: number;
  // #177 S6 — additive. `kind` echoes the requested facet; `search_depth`
  // is 'prose-only' while the one-time index split is still backfilling on
  // this install (tools/thinking facets return empty there), else 'full'.
  kind?: SearchKind;
  search_depth?: 'prose-only' | 'full';
}

export interface ConversationJump {
  session_id: string;
  uuid: string;
  // #177 S6 — when the matched anchor carried a tool/thinking match the find
  // bar sets this so the reader opens the target turn's collapsed <details>
  // disclosures before scrolling (the client can't know which disclosure holds
  // the needle, so it opens them all — bounded + predictable). Absent on every
  // other jump (search-hit click, outline jump, jump-to-next): the reader's
  // jump effect only expands when this is truthy.
  expand_details?: boolean;
}

// #177 S6 — one rendered-turn anchor for the in-conversation find bar.
// `uuid` is the rendered item's anchor uuid (directly in the reader's
// itemRefs — no member resolution needed); `match_kinds` aggregates the
// non-prose match labels across the turn's matched member rows (sorted
// lowercase; empty for a prose-only match).
export interface FindAnchor {
  uuid: string;
  match_kinds: ('tool' | 'thinking')[];
}

// #177 S6 — GET /api/conversation/<id>/find response. Bound field-for-field
// to find_in_conversation in bin/_lib_conversation_query.py. `anchors` are
// document-ordered; `total` counts rendered-turn anchors PRE-cap (the list
// caps at 500 with `anchors_truncated: true`). `search_depth` mirrors the
// rail search interim signal (tools/thinking facets return empty anchors
// while 'prose-only').
export interface ConversationFindResult {
  anchors: FindAnchor[];
  total: number;
  anchors_truncated: boolean;
  mode: 'fts' | 'like';
  search_depth: 'prose-only' | 'full';
}

// #178 on-demand "load full" route response, discriminated on `which` (spec
// §4.4 / §4.6). Bound field-for-field to read_full_payload in
// bin/_lib_conversation_query.py:
//   which='result' → { which, tool_use_id, text, full_length, truncated,
//                       is_error, [stderr] } — the full _stringify(content),
//                       plus the full Bash stderr when present.
//   which='input'  → { which, tool_use_id, input, full_length, truncated } —
//                       the full structured input dict (so the DiffCard can pull
//                       old_string/new_string straight into computeDiff).
// `full_length`/`truncated` describe the serialized payload against the route's
// 1 MB ceiling. All additive; consumers tolerate absence of optional keys.
export type FullPayload =
  | {
      which: 'result';
      tool_use_id: string;
      text: string;
      full_length: number;
      truncated: boolean;
      is_error: boolean; // read_full_payload ALWAYS emits this on the result branch
      stderr?: string | null;
    }
  | {
      which: 'input';
      tool_use_id: string;
      input: Record<string, unknown>;
      full_length: number;
      truncated: boolean;
    };
