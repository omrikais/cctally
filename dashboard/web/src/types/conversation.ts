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

export interface CodexLifecycleState {
  schema_version: 1;
  state: string;
  started?: Record<string, unknown>;
  completed?: Record<string, unknown>;
  events: Array<{ event: string; payload_which: 'event'; block_key: string }>;
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
      // The TURN's cost, counted ONCE (0.0 for null msg_id). NULL means "this
      // item has no cost of its own", which #463 S1 segmentation introduces: the
      // turn stays the costing unit, so its carrier segment reports the cost and
      // every other segment reports null. A zero would be indistinguishable from
      // a genuinely free turn, so consumers must test `> 0` or `!= null`, never
      // coerce through a numeric default.
      cost_usd: number | null;
      // #463 S1 — turn membership for a segmented item. Equal to segment 0's key,
      // so grouping on it recovers the turn without recomputing boundaries.
      // Absent on an envelope from a server that predates segmentation.
      turn_uuid?: string;
      // #463 S1 — 0 for a whole item and for a turn's first segment.
      segment_ordinal?: number;
      // #177 S1 backend / S5 client adoption (Codex F7) — per-turn token usage,
      // stamped when the turn key has a session_entries row. Absent (NOT
      // zero-filled) otherwise; the §6 footer reads it.
      tokens?: TokenUsage;
      // #334 Task B — folded Codex lifecycle evidence stays attached to its
      // owning logical response but does not create a standalone reader row.
      lifecycle?: CodexLifecycleState;
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
      cost_usd?: number | null; // present (0.0) only on the null-msg_id assistant case
      // #463 S1 — segmentation metadata, threaded onto every adapted item.
      turn_uuid?: string;
      segment_ordinal?: number;
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
      lifecycle?: CodexLifecycleState;
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
      // #463 S1 — segmentation metadata, threaded onto every adapted item. A
      // non-response item is always exactly one segment, so `segment_ordinal`
      // is 0 here and `turn_uuid` equals the item's own key.
      turn_uuid?: string;
      segment_ordinal?: number;
      ts: string;
      text: string;
      blocks: ConversationBlock[];
      is_sidechain: boolean;
      subagent_key: string | null;
      parent_uuid: string | null;
      meta_kind: 'skill' | 'command' | 'context' | 'compaction' | 'notification';
      skill_name: string | null;
      // Qualified providers preserve the native semantic source instead of
      // collapsing every injected row into generic context/notification copy.
      // Optional so the established Claude envelope remains byte-compatible.
      meta_label?: string | null;
      meta_sections?: string[];
    };

// #177 S1 backend / S5 client adoption — per-turn token usage, stamped on
// assistant turn items when the turn key has a session_entries row. Absent
// (NOT zero-filled) otherwise. Shared by the detail item and the outline turn.
export interface TokenUsage {
  input: number;
  output: number;
  cache_creation: number;
  cache_read: number;
  // Qualified Codex detail keeps its native vocabulary. The legacy cache fields
  // remain zero on Codex values for arithmetic compatibility, but renderers must
  // branch on `source` and show cached-input/reasoning-output instead.
  source?: 'claude' | 'codex';
  cached_input?: number;
  reasoning_output?: number;
}

// #177 S5 — GET /api/conversation/<id>/outline (spec §1). `ts` nullable (F6).
export interface OutlineToolRef { name: string | null; is_error: boolean; }
export interface OutlineTurn {
  uuid: string;
  kind: 'assistant' | 'human' | 'tool_result' | 'meta';
  ts: string | null;
  label: string;
  member_uuids: string[];
  // #463 S1 — the keys of this turn's segments, entry `i` being segment `i`.
  // DISTINCT from member_uuids on purpose: `loadToTarget` treats a uuid in
  // member_uuids as already loaded, so folding segment keys in would make the
  // drain skip an unfetched segment and the jump would land nowhere.
  segment_uuids?: string[];
  subagent_key: string | null;
  parent_uuid: string | null;
  is_sidechain: boolean;
  model?: string;
  tokens?: TokenUsage;
  tools?: OutlineToolRef[];
  // #463 S4 §4.4 — the two facts dedupe destroys. `tools` is deduplicated by
  // name because a Codex turn can carry 523 calls, which makes `tools.length`
  // no longer the call count and moves the first entry carrying `is_error` away
  // from the first call that actually failed. Consumers that need either read
  // these instead. Absent on a turn with no calls.
  tool_call_count?: number;
  first_failure_name?: string | null;
  thinking?: string[];
  meta_kind?: 'skill' | 'command' | 'context' | 'compaction' | 'notification';
  skill_name?: string | null;
  meta_label?: string | null;
  meta_sections?: string[];
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
// #463 S4 §3.2 — tier 2. A landmark is a place inside a turn worth reaching:
// an authored reasoning heading, a failing tool call, or a plan call. It is
// deliberately NOT one entry per tool call, because a 523-call turn would
// contribute 523 rows, which is noise rather than navigation.
export type OutlineLandmarkKind = 'reasoning' | 'tool_error' | 'plan';
export interface OutlineLandmark {
  // Stable identity, `<block_key>#<discriminator>` — a heading's zero-based
  // ordinal, or the kind. `block_key` alone collides: one reasoning block
  // yields several headings, and one failing plan call is two landmarks.
  landmark_key: string;
  block_key: string;
  // The SEGMENT that contains the landmark, which is what a jump loads. A turn
  // key would land on segment 0, and S1 defined that as the start of a turn
  // whose failure may be fifteen segments later.
  uuid: string;
  // The owning tier-1 turn, which drives both the indentation and the §1.3
  // retention rule. `segmentIndex` cannot supply it — it is built from
  // SURVIVING turns' segment keys and is empty on real Codex data.
  parent_uuid: string;
  kind: OutlineLandmarkKind;
  label: string;
  ts: string | null;
}
export interface OutlineStats {
  turns: { total: number; human: number; assistant: number; tool_result: number; meta: number };
  tool_counts: Record<string, number>;
  // #463 S4 D3 — NULLABLE, following `duration_seconds` on this same object. A
  // conversation whose retained event payloads are gone has outcome-bearing
  // rows and no verdict on any of them, and rendering 0 there would assert an
  // absence nobody proved.
  error_count: number | null;
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
// #217 S5 F2 — one Edit/MultiEdit/Write call against a file (a "touch"). `add`/
// `del` are integers when the stat is known (stamped edit_stat or recomputed),
// else null — the touch is STILL listed (Codex P1-7). `tool_use_id` is carried
// for a future precise-diff scroll; `uuid` is the turn anchor (the jump target).
// #463 S4 §6.6 — the op vocabulary spans two providers. Claude touches carry
// the edit-family TOOL (`edit`/`multiedit`/`write`); a Codex touch carries the
// raw CHANGE KIND the patch payload stated — `add`/`delete`/`update` from the
// dict shape and `modified` from the list one — and `null` when the provider
// stated neither, which the server publishes rather than guessing.
export type OutlineFileOp =
  | 'edit' | 'multiedit' | 'write'
  | 'add' | 'delete' | 'update' | 'modified'
  | null;
export interface OutlineFileTouch {
  uuid: string;
  tool_use_id: string | null;
  op: OutlineFileOp;
  add: number | null;
  del: number | null;
}
// #217 S5 F2 — one modified file: per-path summed +N/-M over its touches
// (null when every touch's stat is unknown), in first-touch document order.
export interface OutlineFile {
  path: string;
  add: number | null;
  del: number | null;
  touches: OutlineFileTouch[];
}
// Provider-neutral S7 file fact. Unlike Claude edit-family touches, Codex
// normalization retains the native tool + count but no trustworthy turn
// anchor or +/- stat. Keep that distinction explicit instead of fabricating
// jump targets to fit OutlineFile.
export interface QualifiedOutlineFile {
  path: string;
  tool: string;
  count: number;
}
// #217 S5 F7 — main-thread task-completion. The server emits this whenever the
// MAIN thread carried any task snapshot (Task* / legacy TodoWrite); the client
// renders the chip + outline landmark ONLY when `all_done`. `anchor_uuid` is the
// turn carrying the final snapshot (the jump target). `null` (not the object)
// when the session has no main-thread tasks.
export interface OutlineTaskCompletion {
  all_done: boolean;
  total: number;
  completed: number;
  anchor_uuid: string;
}
export interface ConversationOutline {
  session_id: string;
  subagent_meta?: Record<string, SubagentMeta>;
  // #217 S3 E6(a) — display-only per-subagent cost map (subagent_key → USD),
  // summed cost-once on the server. Kept SEPARATE from subagent_meta so the
  // outline↔reader subagent_meta parity stays byte-for-byte; covers every
  // subagent bucket including ones with empty subagent_meta. Optional for
  // back-compat with a payload from an older server.
  subagent_costs?: Record<string, number>;
  stats: OutlineStats;
  // #217 S5 F2 — whole-session files-touched aggregation. Always present from a
  // current server (possibly []); optional for back-compat with an older one.
  files?: OutlineFile[];
  provider_files?: QualifiedOutlineFile[];
  // #217 S5 F7 — main-thread task-completion (null when no main-thread tasks).
  // Present from a current server; optional for back-compat with an older one.
  task_completion?: OutlineTaskCompletion | null;
  turns: OutlineTurn[];
  // #463 S4 §3.3 — tier 2, deliberately a SEPARATE array. `stats.turns.*` is
  // derived by filtering `turns` on kind, so folding landmarks in would inflate
  // counts meant to describe the conversation's structure. Codex-only; the
  // client tolerates its absence, which is what keeps Claude unchanged.
  landmarks?: OutlineLandmark[];
  // #463 S1 — document position of every addressable key (turn key, folded
  // member key, segment key) over the FULL wire turn list. `turns` above is the
  // NAVIGATION subset: the qualified adapter drops meta turns and event-bearing
  // non-compaction turns, which on real Codex data removes every heavy assistant
  // response and therefore every multi-segment turn. `loadToTarget` needs a total
  // order to pick a paging direction, and the navigation subset is not one — a
  // target inside a dropped turn resolved to no index at all, so the drain
  // returned before its first page. Absent for providers whose outline carries
  // no positional wire data; `loadToTarget` then falls back to the skeleton index.
  positionByKey?: ReadonlyMap<string, number>;
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

export interface NativeTerminalCommand {
  command: string;
  workdir: string | null;
  metadata: Record<string, unknown>;
}

export interface NativeTerminalOutput {
  schema_version: 1;
  type: 'terminal_output';
  status: string;
  is_error: boolean;
  parts: { type: 'text' | 'raw'; stream: 'stdout' | 'stderr' | 'output'; text: string }[];
  truncated: boolean;
  // #463 S3 §4.3 — recovered from the harness preamble. Optional here rather
  // than required: a server that predates S3 publishes neither, and absence
  // must degrade to today's rendering instead of asserting a null value the
  // server never claimed.
  exit_code?: number | null;
  wall_time_seconds?: number | null;
}

// #463 S3 — a call-side `apply_patch` entry is a file LIST, not a diff. The
// wire contract (§3.5) states it carries no `unified_diff`, no `diff_source`
// and no per-file `truncated`, so it gets its own narrower type: sharing one
// type with the event family would let a renderer read a diff key off an entry
// that structurally cannot have one.
export interface NativePatchRequestFile {
  path?: string;
  move_path?: string;
  status?: string;
}

// The event-side (`patch_apply_end`) entry family. `truncated` means THIS
// FILE's own text was cut, independently of the card-level `truncated`; it may
// be absent on an entry from a pre-S3 server and reads as false. `truncated`
// true with no `unified_diff` is a real state and means "there is a diff and
// none of it survived the budget" (wire contract §4).
export interface NativePatchFile extends NativePatchRequestFile {
  truncated?: boolean;
  unified_diff?: string;
  // 'retained' = the provider transmitted the diff; 'derived' = the server
  // synthesized it from retained file content. A derived diff must never be
  // presented as provider-supplied.
  diff_source?: 'retained' | 'derived';
  raw?: string;
  raw_extra?: string;
}

export interface NativeResultEnvelope {
  status: string;
  value: unknown;
  truncated: boolean;
}

export interface NativeWebSearchResult {
  title: string;
  url: string;
  domain?: string;
  snippet?: string;
  ref_id?: string;
  type?: string;
}

export type NativeAgentOperation =
  | 'spawn_agent'
  | 'wait_agent'
  | 'send_message'
  | 'list_agents'
  | 'followup_task'
  | 'interrupt_agent';

// #463 S3 §3.3 — one `program` card entry, discriminated on `kind`. `session`
// repeats a `session_ref` card's fields minus the envelope; `other` names a
// tool the scanner located but whose arguments the closed literal parser
// declined, so the card claims nothing about what it was given.
export type NativeProgramInvocation =
  | { kind: 'command'; command: string; workdir: string | null; metadata: Record<string, unknown> }
  | { kind: 'session'; scope: 'shell' | 'cell'; ref: string | null; operation: 'write' | 'poll'; chars: string | null }
  | { kind: 'other'; name: string };

// #463 S3 §3.2 — the conversation-scoped shell-session index published beside
// `items`. Keyed by the ordinal in decimal, which is exactly what a
// `session_ref` card's `ref` carries at `shell` scope, so a reference resolves
// by direct lookup and never by a scan. Ordinals are assigned server-side over
// the whole conversation: the client never computes uniqueness, ordering or
// shortening from a loaded window.
export interface ConversationSessionIndex {
  sessions: Record<string, { ordinal: number; opener_block_key: string | null }>;
  // true when the conversation held more sessions than the server's index cap.
  // A missing entry then means "not loaded", NOT "absent" — the two must stay
  // distinguishable in the UI.
  truncated: boolean;
}

// #463 S3 §5.1 — evidence-based result state for a Codex tool call, derived
// client-side from the result-side `terminal_output` card and published
// independently of whether the CALL side validated as a native card. There is
// no `outcome` object on the wire.
export interface ToolOutcome {
  status: 'completed' | 'failed' | 'running' | 'unknown';
  exit_code: number | null;
  wall_time_seconds: number | null;
}

export type NativeToolCard =
  | {
      schema_version: 1;
      type: 'terminal';
      status: string;
      commands: NativeTerminalCommand[];
      output?: NativeTerminalOutput;
      truncated?: boolean;
    }
  | {
      schema_version: 1;
      type: 'patch';
      source: string;
      status: string;
      files: NativePatchFile[];
      patch?: string;
      request_files?: NativePatchRequestFile[];
      success?: boolean | null;
      stdout?: string | null;
      stderr?: string | null;
      has_diff?: boolean;
      truncated?: boolean;
      event_payload_key?: string;
    }
  | {
      schema_version: 1;
      type: 'plan';
      source: 'update_plan';
      call_status: string;
      explanation: string | null;
      items: { step: string; status: string }[];
      result?: NativeResultEnvelope;
    }
  | {
      schema_version: 1;
      type: 'web_search';
      source: 'web_search_call';
      call_status: string;
      query: string;
      action: Record<string, unknown>;
      completion: {
        status: string;
        query: string;
        action: Record<string, unknown>;
        results: NativeWebSearchResult[];
        error?: string;
        event_block_key?: string;
      };
    }
  | {
      schema_version: 1;
      type: 'mcp';
      source: 'function_call';
      name: string;
      call_status: string;
      completion: {
        status: string;
        server: string;
        tool: string;
        arguments: Record<string, unknown>;
        result: Record<string, unknown>;
        duration: { secs: number; nanos: number };
        event_block_key?: string;
      };
    }
  | {
      schema_version: 1;
      type: 'agent';
      operation: NativeAgentOperation;
      call_status: string;
      arguments: Record<string, unknown>;
      result?: NativeResultEnvelope;
      child_conversation?: {
        conversation_key: string;
        role?: string;
        nickname?: string;
      };
    }
  // #463 S3 §3.3 — a JavaScript program rather than the strict command chain.
  // `complete: false` means the scanner located these invocations and the body
  // also contains statements it did not read, so the listed invocations are
  // NEVER the whole program.
  | {
      schema_version: 1;
      type: 'program';
      title: string | null;
      complete: boolean;
      invocations: NativeProgramInvocation[];
      truncated: boolean;
    }
  // #463 S3 §3.2 — a `write_stdin` (shell) or `wait` (cell) reference. The two
  // namespaces have zero overlapping values: a cell reference is never a shell
  // session, is never rendered with the session badge, and is never grouped by.
  // `ref` null at `shell` scope renders NO badge — the server has no ordinal to
  // publish and the client must never invent one.
  | {
      schema_version: 1;
      type: 'session_ref';
      scope: 'shell' | 'cell';
      ref: string | null;
      operation: 'write' | 'poll';
      chars: string | null;
      truncated: boolean;
    }
  | {
      schema_version: 1;
      type: 'tool_search';
      query: string;
      limit: number | null;
      truncated?: boolean;
    };

export type ConversationBlock =
  // #463 S2 §3.2 — `block_key` is the server's durable per-row anchor, retained
  // so each separately authored message keeps its identity through adaptation.
  // Absent on a wire envelope from a server that predates #463 S2 §1.
  | { kind: 'text'; text: string; block_key?: string }
  | { kind: 'thinking'; text: string }
  | {
      // #334 Task B — provider-native reasoning is deliberately distinct from
      // Claude's Thinking disclosure. Every prose field is optional on the
      // wire, but the adapter drops a block unless at least one is non-empty.
      kind: 'codex_reasoning';
      source: string;
      title?: string;
      summary?: string;
      body?: string;
      // #463 S2 §2.5/§2.6 — the individual authored headings the aggregate holds,
      // decomposed server-side at read time from the retained summary entries.
      // `key` is `<block_key>#<zero-based ordinal>`. Absent whenever the server
      // could not read the retained payload, in which case the reader falls back
      // to `summary`/`title` exactly as before.
      headings?: { key: string; text: string }[];
      // #463 S4 F-A — the physical row's own key, retained so a tier-2 jump can
      // address this block in the DOM, and so occurrence-exact find can name the
      // container a fragment lives in. Codex only: the Claude projection
      // publishes no block keys, so the anchor wrapper is never rendered there.
      block_key?: string;
    }
  | {
      kind: 'system_actions';
      actions: Array<
        | { type: 'git'; action: 'create_branch' | 'stage' | 'commit' | 'push' | 'create_pr'; draft?: boolean }
        | { type: 'memory_citation'; citation_count: number; rollout_count: number }
      >;
      payload_key?: string;
    }
  | {
      kind: 'codex_lifecycle';
      event: 'task_started' | 'task_complete';
      message?: string;
      error?: string;
      duration_ms?: number;
      payload_key?: string;
    }
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
      // #463 S4 F-A — the physical row's own key, retained so a tier-2 jump can
      // address this block in the DOM, and so occurrence-exact find can name the
      // container a fragment lives in. Codex only: the Claude projection
      // publishes no block keys, so the anchor wrapper is never rendered there.
      block_key?: string;

      // Qualified Codex blocks can always re-read their call/output through the
      // opaque block_key payload route, even when the bounded detail projection
      // does not claim truncation.
      payload_capable?: boolean;
      // #331 Session B — provider-native card projection. The provider tool
      // name remains unchanged (`exec`, `apply_patch`, `patch_apply_end`); this
      // additive shape selects presentation without relabelling the record.
      native_card?: NativeToolCard;
      // #463 S3 §5.1 — the result-side structure, published INDEPENDENTLY of
      // whether the call side validated as a native card, and gated on
      // `source === 'codex'`. `native_card` keeps its meaning as a validated
      // CALL-side structure; this is a separate additive channel, so nothing
      // reading `native_card` changes behavior. Absent for Claude and for a
      // Codex call whose result carried no readable `terminal_output` card.
      outcome?: ToolOutcome;
      payload_kind?: 'call' | 'event';
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
      web_search?: { query: string; links: NativeWebSearchResult[]; links_truncated?: boolean };
      web_fetch?: { code: number; code_text?: string };
      // Backgrounded-MCP recovery (spec 2026-07-31 §5). Claude Code moves an MCP
      // call that exceeds 120s to a background task and leaves a "still running
      // after 120s" placeholder as the result; the completion arrives later as a
      // separate attachment record the kernel joins back here.
      //
      // `background_status` is stamped on EVERY backgrounded call, recovered or
      // not, carrying whatever the notification claimed ('running' when there is
      // no notification at all). It is NOT the recovered-predicate: a completion
      // that carried no <result>, and an ambiguity whose candidates are all
      // completed, both legitimately say "completed" while `result` is still the
      // placeholder.
      //
      // `background_completed_at` is written ONLY by a successful join, so it —
      // and only it — means the result below is the real response. Renderers
      // MUST key "recovered" off this field; keying off the status reports false
      // success over placeholder text. Both absent on every non-background call.
      background_status?: string;
      background_completed_at?: string;
    }
  // 'tool_result' BLOCK kind survives ONLY inside a standalone orphan
  // tool_result ITEM (a result the kernel could not fold into a request).
  // #177 S4 — orphan results keep `tool_use_id` + `media` so their screenshots
  // still render (the kernel surfaces media on the standalone result block).
  // #463 S4 remediation C-4 — `block_key` is the row's own jump address. An
  // unfolded failing `tool_output` is its own group head and therefore its own
  // `tool_error` landmark, so the outline publishes that key as the address a
  // jump aligns; without it on the block the chip rendered no `data-block-key`
  // and the landmark named nothing in the DOM.
  | { kind: 'tool_result'; text: string; truncated: boolean; is_error: boolean; tool_use_id?: string | null; media?: MediaRef[]; block_key?: string }
  // #177 S4 — `index` is the ingest-stamped media ordinal (the uuid-mode route
  // address); absent on pre-reingest rows → the figure degrades to the badge.
  | { kind: 'image'; media_type: string | null; bytes: number; index?: number }
  | { kind: 'document'; media_type: string | null; bytes: number; index?: number }
  // #463 S3 §5.5 — the external-agent marker, detected server-side at read time
  // over an assistant row's stored text. It is NOT a tool_call: it never enters
  // chips, filters, the Files tab or the outline. The raw marker prose stays in
  // the row's `text` (the export bytes are frozen), so the adapter removes the
  // server-published span from the prose block it emits alongside this one —
  // otherwise the reader would see the marker twice.
  | { kind: 'external_call'; name: string; input: unknown; truncated: boolean; block_key?: string }
  | { kind: 'tool_reference'; name: string | null };

export interface ConversationSummary {
  conversation_ref?: ConversationRef;
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
  // #501 — an exact selected conversation projected independently of the
  // paginated/filter-derived rows. The client pins it only when no user filter
  // is active and deduplicates it when the ordinary page already contains it.
  selected?: ConversationSummary;
  // `filter_degraded` (filters spec §1 dual-branch parity) is present ONLY when a
  // project/cost/rebuild filter was requested but the rollup was non-authoritative
  // (the live `GROUP BY` fallback can only filter by date). `sort_degraded` (#217
  // S4 / I-2.1) is present ONLY when a `cost`/`project` sort was requested under
  // the same non-authoritative window (the live fallback has no such column, so
  // the page fell back to `recent` order — keyed to the REQUESTED sort, never an
  // unknown sort). The rail surfaces each as a muted note; absent on the normal
  // authoritative path.
  page: {
    next_offset: number | null;
    has_more: boolean;
    filter_degraded?: boolean;
    sort_degraded?: boolean;
  };
}

// #217 S4 / I-2 — rail sort keys, 1:1 with the backend `_SORTS` table
// (bin/_lib_conversation_query.py). `recent` (default) / `oldest` ride stored
// columns on every branch; `cost`/`messages`/`project` are rollup-column sorts
// (`messages` has live parity, `cost`/`project` degrade to `recent` + the
// page's `sort_degraded` flag in the brief non-authoritative window).
export type RailSortKey = 'recent' | 'oldest' | 'cost' | 'messages' | 'project';

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
  // #278 Theme C — selected model families (opus/sonnet/haiku/fable). OR within
  // the axis, AND across axes — exactly like `projects`. Empty = axis inactive.
  models: string[];
}

export const EMPTY_FILTERS: ConversationFilters = {
  dateFrom: null, dateTo: null, datePreset: null,
  projects: [], costMin: null, costMax: null, rebuildMin: null, models: [],
};

// GET /api/conversations/facets — sorted distinct project labels + per-label
// conversation count, for the filter popover's project multi-select. #278 Theme
// C adds `models`: per-model-family session counts (families-present-only,
// excluding 'other', in the backend's fixed opus/sonnet/haiku/fable order).
export interface ConversationFacets {
  projects: { project_label: string; count: number; filter_value?: string }[];
  models: { family: string; count: number; filter_value?: string }[];
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
  // #217 S3 E2 — the bidirectional windowed pager. `next_after`/`has_more`
  // describe the BOTTOM edge (forward paging + the live-tail gate); the two
  // additive `prev_before`/`has_prev` keys describe the TOP edge (reverse
  // paging). `has_prev = start > 0`; `prev_before` = the id of the page's FIRST
  // item when `has_prev`, else null (the cursor for the next `?before=`). Optional
  // here for back-compat with fixtures/responses predating the keys (a missing
  // `has_prev` reads as a single-page / no-top-edge open). See
  // bin/_lib_conversation_query.py::get_conversation.
  page: { next_after: number | string | null; has_more: boolean; prev_before?: number | string | null; has_prev?: boolean };
  subagent_meta?: Record<string, SubagentMeta>;  // keyed by subagent_key (#166)
  // #463 S3 §3.2 — whole-conversation shell-session index, re-sent on every
  // page like `subagent_meta`. Absent from a server that predates S3 and from
  // every Claude response, in which case no session badge renders at all.
  session_index?: ConversationSessionIndex;
  // jump-to-latest spec §3 — the conversation's final RENDERED turn (the last
  // grouped/deduped item, not the last raw JSONL row). Constructed explicitly by
  // the server with the request session_id (the assembled item's anchor carries a
  // null session_id, Codex P2 #4). `null` only for a genuinely empty conversation
  // (the Jump-to-latest control hides). Task 4 consumes it.
  last_anchor?: { session_id: string; uuid: string; id: number } | null;
  // Provider-neutral metadata that has no honest legacy-Claude analogue. The
  // shared reader uses this only for source labels, Codex parent/child links,
  // native token totals, and unattributed cost; Claude responses omit it.
  provider_meta?: {
    source: ConversationSource;
    conversation_key: string;
    tokens?: TokenUsage | null;
    unattributed_cost_usd?: number;
    parent?: { conversation_key: string; title: string | null } | null;
    children?: { conversation_key: string; title: string | null; cost_usd: number }[];
  };
}

// #177 S6 — kind facet for the rail search chips. Maps 1:1 to the backend
// `_SEARCH_KINDS` param. #217 S4 / I-3.1 — the two cross-session STRUCTURAL
// facets `title` (session-title search) and `files` (file-path search) join the
// five content kinds (7 total, matching `_SEARCH_KINDS` in
// bin/_lib_conversation_query.py). The find bar's `_CONV_FIND_KINDS` stays the
// five content kinds — title/files are search-only (the find bar searches within
// one open conversation, not session titles / file paths).
export type SearchKind =
  | 'all' | 'prompts' | 'assistant' | 'tools' | 'thinking' | 'title' | 'files';

export interface SearchHit {
  conversation_ref?: ConversationRef;
  session_id: string;
  uuid: string;
  project_label: string;
  title: string; // derived conversation title for the hit's session (#165 Q4)
  ts: string;
  snippet: string;
  cost_usd: number;
  // #177 S6 — non-prose match badges (sorted lowercase; server omits prose).
  // Always present on the wire (defaults to []); optional here for back-compat
  // with fixtures predating the field. #217 S4 / I-3.1 — the cross-session facets
  // add `title` (a kind=title hit) and `file` (a kind=files hit).
  match_kinds?: ('tool' | 'thinking' | 'title' | 'file')[];
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
  // #217 S4 / I-2.5 — present ONLY when a project/cost/rebuild filter was
  // requested under the non-authoritative window (the live fallback can only
  // filter by date). NOTE: unlike the browse page's `filter_degraded` (nested
  // under `page`), the SEARCH response carries this flag TOP-LEVEL. The rail
  // surfaces it as a "some filters unavailable while indexing" note.
  filter_degraded?: boolean;
}

export interface ConversationJump {
  // Qualified identity is authoritative for new client paths. `session_id`
  // remains the legacy Claude compatibility field while bare links/actions are
  // still accepted during the Task A migration.
  conversation_ref?: ConversationRef;
  session_id: string;
  uuid: string;
  // #177 S6 — when the matched anchor carried a tool/thinking match the find
  // bar sets this so the reader opens the target turn's collapsed <details>
  // disclosures before scrolling (the client can't know which disclosure holds
  // the needle, so it opens them all — bounded + predictable). Absent on every
  // other jump (search-hit click, outline jump, jump-to-next): the reader's
  // jump effect only expands when this is truthy.
  expand_details?: boolean;
  // #463 S4 F-A — the addressable element INSIDE the loaded item that this jump
  // is really about: a landmark's rendered block (`data-block-key`) or one
  // decomposed reasoning heading (`data-heading-key`). Absent on every jump
  // whose target is a whole turn, and a key that resolves to nothing degrades to
  // aligning the item, which is the pre-S4 landing.
  inner_anchor_key?: string;
  find_occurrence?: FindOccurrence;
}

export function conversationJumpRef(jump: ConversationJump): ConversationRef {
  return jump.conversation_ref ?? legacyClaudeConversationRef(jump.session_id);
}

// #463 S4 remediation round 3 — the ONE place a jump payload is constructed.
//
// The rule this work established is that the rail row, the cluster chip and the
// reader key must send an IDENTICAL payload for the same target, and until now
// three literal object constructions held that rule up with a three-way equality
// test as the only guard. A fourth field would have had to be threaded to three
// places again, and the round that added `inner_anchor_key` reached two of them
// and left the third — which is the defect the browser gate found.
//
// Round 4 completed the sweep: the rail's search-hit row, the comparison view's
// open-in-reader action and the find bar were still writing their own literals,
// so the guarantee this builder exists to provide did not hold and both the
// comment here and `docs/dashboard-gotchas.md` asserted an absolute that was
// false. `jumpConstruction.test.ts` is the static scan that now backs it: no
// production source file outside this one may write a jump object literal.
//
// Round 5 — the scan's exact reach is documented in its own header, and it is
// not total. It catches a `jump:` literal (including one whose brace is on a
// later line), a typed `: ConversationJump = {` local, any `as`/`satisfies
// ConversationJump` assertion, and a local named `jump` holding an object
// literal, all after stripping comments. It cannot catch a literal bound to a
// differently-named variable and passed as `jump: thatName`. Payload assertions
// at the dispatch sites (`ConversationRail.test.tsx`, `ComparisonView.test.tsx`)
// pin what this builder actually produces, so changing a default below reddens
// a test rather than silently changing a jump.
//
// The builder lives beside the interface so the field list and the construction
// of it cannot drift. `qualified` stays a caller decision rather than being
// derived from the ref: a bare Claude session id opened through the legacy path
// must keep sending `session_id` alone, and only the caller knows which form its
// own input took.
//
// Round 7 — the three optional inputs are ONE OPTIONS OBJECT rather than
// positional parameters 4-6, because they do not share an omission rule and the
// positional form hid that. `innerAnchorKey` and `findOccurrence` are gated on
// being TRUTHY; `expandDetails` is gated on being SUPPLIED. Positionally, a
// caller wanting only an occurrence had to write
// `(ref, uuid, qualified, null, undefined, occ)`, and writing `false` in that
// fifth slot instead of `undefined` silently added `expand_details: false` to
// the payload — TypeScript accepts both, and the two spellings differ only in a
// key the reader reads for truthiness. Named, each rule is visible where it is
// used and an absent option is written by being absent. The three gates
// themselves are unchanged, so every payload is byte-identical to the positional
// form; `conversationJump.test.ts` proves that against a verbatim copy of the
// pre-round-7 builder for all fifteen production call-site shapes, and asserts
// the exact KEY SET each option combination produces so a future divergence
// reddens a test.
//
// Why the two gates differ: an anchor key is an address, so an empty one is an
// absent one, and an occurrence is a whole object. `expand_details` is a boolean
// the find bar has always written on every jump it issues, including as `false`,
// and omitting the key there would change a payload three tests assert
// byte-for-byte while changing no behavior. Supplying no option at all still
// omits every key, so a caller that passes nothing is unchanged.
//
// `findOccurrence` arrived on `main` with occurrence-exact find (#463 S4 merge).
// It was introduced as a literal `jump: { … find_occurrence }` in the find bar,
// written before this builder existed on that branch; routing it through the
// builder here is what keeps `jumpConstruction.test.ts` true rather than making
// the find bar the one production exception to it.
export interface ConversationJumpOptions {
  /** Omitted from the payload when falsy — an empty address is no address. */
  innerAnchorKey?: string | null;
  /** Omitted only when NOT SUPPLIED; an explicit `false` IS emitted. */
  expandDetails?: boolean;
  /** Omitted from the payload when falsy. */
  findOccurrence?: FindOccurrence | null;
}

export function buildConversationJump(
  ref: ConversationRef, uuid: string, qualified: boolean,
  options: ConversationJumpOptions = {},
): ConversationJump {
  const { innerAnchorKey, expandDetails, findOccurrence } = options;
  return {
    ...(qualified ? { conversation_ref: ref } : {}),
    session_id: ref.key,
    uuid,
    ...(innerAnchorKey ? { inner_anchor_key: innerAnchorKey } : {}),
    ...(expandDetails === undefined ? {} : { expand_details: expandDetails }),
    ...(findOccurrence ? { find_occurrence: findOccurrence } : {}),
  };
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
export interface LegacyConversationFindResult {
  anchors: FindAnchor[];
  total: number;
  anchors_truncated: boolean;
  // #217 S4 — `regex` joins the union when the find ran a regex scan
  // (`like` for a case-only scan, `fts` for the default FTS/LIKE fast path).
  mode: 'fts' | 'like' | 'regex';
  search_depth: 'prose-only' | 'full';
}

export interface FindFragment {
  leaf_key: string;
  start: number;
  end: number;
}

export interface FindOccurrence {
  occurrence_id: string;
  item_key: string;
  uuid: string;
  block_key: string;
  container_block_key: string;
  surface: 'body' | 'call' | 'output' | 'completion';
  match_kinds: ('tool' | 'thinking')[];
  disclosure: string[];
  fragments: FindFragment[];
}

export interface OccurrenceFindResult {
  schema_version: 2;
  semantics: 'occurrence';
  status: 'ready' | 'indexing';
  query_id: string;
  total?: number;
  selection_stale: boolean;
  mode: 'literal' | 'regex';
  kind: string;
  search_depth: 'prose-only' | 'full';
  page: {
    start_index: number;
    previous_cursor: string | null;
    next_cursor: string | null;
    occurrences: FindOccurrence[];
  };
}

export type ConversationFindResult = LegacyConversationFindResult | OccurrenceFindResult;

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
      // #217 S1 / U5 (data-contract honesty): `truncated` describes ONLY the
      // `text` stream; the Bash `stderr` stream is bounded INDEPENDENTLY against
      // the same ceiling, so this dedicated flag reports whether stderr itself was
      // clipped. Emitted only when `stderr` is present. Forward-compat: no UI
      // consumer is wired yet (a later frontend session surfaces it).
      stderr_truncated?: boolean;
    }
  | {
      which: 'input';
      tool_use_id: string;
      input: Record<string, unknown>;
      full_length: number;
      truncated: boolean;
    }
  | {
      which: 'event';
      tool_use_id: string;
      text: string;
      full_length: number;
      truncated: boolean;
      card?: NativeToolCard;
    };

// #217 S3 E1 — anchor-based reading-position memory. The persistence module
// records the CURRENT TURN uuid (the scroll-sync `convCurrentTurnUuid`) per
// session, NOT a pixel offset (with open-at-bottom the loaded item-set + viewport
// differ between visits, so a pixel offset is meaningless). Stored as a bounded
// LRU map (~50 sessions, oldest `ts` evicted) under a namespaced localStorage key.
export interface ReadingPos {
  uuid: string;
  ts: number; // epoch ms — the LRU recency key
}
export type ReadingPosMap = Record<string, ReadingPos>; // keyed by conversationRefKey

// #321 Task A — canonical client identity. `key` is deliberately opaque: it is
// either the S7 `v1.` conversation key or the legacy Claude session id carried
// by the explicit compatibility adapter. Client code compares/serializes the
// pair and never decodes a native UUID or provider root from it.
export type ConversationSource = 'claude' | 'codex';
export interface ConversationRef {
  source: ConversationSource;
  key: string;
  account_key?: string;
}
export type ConversationRefInput = ConversationRef | string;

export function isConversationRef(value: unknown): value is ConversationRef {
  if (!value || typeof value !== 'object') return false;
  const ref = value as Record<string, unknown>;
  return (ref.source === 'claude' || ref.source === 'codex')
    && typeof ref.key === 'string'
    && ref.key.length > 0
    && (ref.account_key === undefined
      || (typeof ref.account_key === 'string' && ref.account_key.length > 0));
}

export function legacyClaudeConversationRef(sessionId: string): ConversationRef {
  return { source: 'claude', key: sessionId };
}

export function normalizeConversationRef(ref: ConversationRefInput): ConversationRef {
  return typeof ref === 'string' ? legacyClaudeConversationRef(ref) : ref;
}

// The entity response shape is selected lexically by the opaque key, not by
// provider. A qualified Claude `v1.` ref uses the same neutral envelopes as a
// qualified Codex ref; only a bare Claude session id uses the legacy shapes.
export function isQualifiedConversationRef(ref: ConversationRefInput): boolean {
  return normalizeConversationRef(ref).key.startsWith('v1.');
}

// JSON tuple framing is injective even when an opaque key contains punctuation
// that would collide under delimiter concatenation.
export function conversationRefKey(ref: ConversationRefInput): string {
  const normalized = normalizeConversationRef(ref);
  return normalized.account_key === undefined
    ? JSON.stringify([normalized.source, normalized.key])
    : JSON.stringify([normalized.source, normalized.key, normalized.account_key]);
}

export function parseConversationRefKey(value: string): ConversationRef | null {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!Array.isArray(parsed) || (parsed.length !== 2 && parsed.length !== 3)) return null;
    const ref = parsed.length === 2
      ? { source: parsed[0], key: parsed[1] }
      : { source: parsed[0], key: parsed[1], account_key: parsed[2] };
    return isConversationRef(ref) ? ref : null;
  } catch {
    return null;
  }
}

export function sameConversationRef(a: ConversationRefInput | null, b: ConversationRefInput | null): boolean {
  if (a === null || b === null) return a === b;
  return conversationRefKey(a) === conversationRefKey(b);
}

export function conversationSummaryRef(row: Pick<ConversationSummary, 'conversation_ref' | 'session_id'>): ConversationRef {
  return row.conversation_ref ?? legacyClaudeConversationRef(row.session_id);
}

export function searchHitConversationRef(hit: Pick<SearchHit, 'conversation_ref' | 'session_id'>): ConversationRef {
  return hit.conversation_ref ?? legacyClaudeConversationRef(hit.session_id);
}

// #217 S3 E2 — the reader's open intent, computed by precedence BEFORE the hook
// fetches so the FIRST request is already correct (no head-fetch-then-redirect
// flash, Codex P1). Precedence: a deep-link/jump anchor (slot 1) > a restored
// E1 reading-position uuid (slot 2) > anchorless tail open (slot 3). A null
// intent (no session) defers the fetch.
export type OpenIntent =
  | { kind: 'anchor'; uuid: string }   // deep-link / jump target
  | { kind: 'restore'; uuid: string }  // saved reading position
  | { kind: 'tail' };                  // open at the bottom (newest)
