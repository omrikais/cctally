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
  | {
      kind: 'tool_call';
      name: string | null;
      input_summary: string;
      preview: string;
      tool_use_id: string | null;
      result: { text: string; truncated: boolean; is_error: boolean } | null;
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
