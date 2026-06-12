// Conversation viewer API types (spec §4 / §5). Bound field-for-field to
// the shipped backend in bin/_lib_conversation_query.py +
// bin/_cctally_dashboard.py. These mirror the three GET routes
// (/api/conversations, /api/conversation/<id>, /api/conversation/search).

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
      meta_kind: 'skill' | 'command' | 'context';
      skill_name: string | null;
    };

// One row of a checklist card (TodoWrite legacy + the live Task* family). The
// shared ChecklistCard renderer normalizes an unknown `status` to 'pending'.
export interface ChecklistTodo {
  content: string;
  status: string;
  activeForm?: string;
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
      preview: string;
      tool_use_id: string | null;
      result: { text: string; truncated: boolean; full_length?: number | null; is_error: boolean } | null;
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
    }
  // 'tool_result' BLOCK kind survives ONLY inside a standalone orphan
  // tool_result ITEM (a result the kernel could not fold into a request).
  | { kind: 'tool_result'; text: string; truncated: boolean; is_error: boolean }
  | { kind: 'image'; media_type: string | null; bytes: number }
  | { kind: 'document'; media_type: string | null; bytes: number }
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
  page: { next_offset: number | null; has_more: boolean };
}

// #166: per-subagent kind + toolUseResult meta, keyed by subagent_key (the same
// agent-file hash the reader groups subagent threads on). Whole-session, present
// on every page (empty case is `{}`). Old transcripts produce no entry for a
// given key → the card falls back to its title-only rendering.
export interface SubagentMeta {
  kind: string;
  total_tokens?: number;
  total_duration_ms?: number;
  total_tool_use_count?: number;
  status?: string;
}

export interface ConversationDetail {
  session_id: string;
  project_label: string;
  git_branch: string | null;
  started_utc: string;
  last_activity_utc: string;
  cost_usd: number;
  models: string[];
  items: ConversationItem[];
  page: { next_after: number | null; has_more: boolean };
  subagent_meta?: Record<string, SubagentMeta>;  // keyed by subagent_key (#166)
}

export interface SearchHit {
  session_id: string;
  uuid: string;
  project_label: string;
  title: string; // derived conversation title for the hit's session (#165 Q4)
  ts: string;
  snippet: string;
  cost_usd: number;
}

export interface ConversationSearchResult {
  query: string;
  mode: 'fts' | 'like';
  hits: SearchHit[];
  total: number;
}

export interface ConversationJump {
  session_id: string;
  uuid: string;
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
      is_error?: boolean;
      stderr?: string | null;
    }
  | {
      which: 'input';
      tool_use_id: string;
      input: Record<string, unknown>;
      full_length: number;
      truncated: boolean;
    };
