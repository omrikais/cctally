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
    };

export type ConversationBlock =
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool_use'; name: string | null; input_summary: string }
  | { kind: 'tool_result'; text: string; truncated: boolean; is_error: boolean }
  | { kind: 'image'; media_type: string | null; bytes: number }
  | { kind: 'document'; media_type: string | null; bytes: number }
  | { kind: 'tool_reference'; name: string | null };

export interface ConversationSummary {
  session_id: string;
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
}

export interface SearchHit {
  session_id: string;
  uuid: string;
  project_label: string;
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
