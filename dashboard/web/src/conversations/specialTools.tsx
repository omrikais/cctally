import type { ReactElement } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { AskUserQuestionCard } from './AskUserQuestionCard';
import { TodoWriteCard } from './TodoWriteCard';
import { ExitPlanModeCard } from './ExitPlanModeCard';
import { DiffCard } from './DiffCard';
import { BashCard } from './BashCard';
import { WebFetchCard } from './WebFetchCard';
import { WebSearchCard } from './WebSearchCard';
import { CodexCard } from './CodexCard';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// Read call.input?.plan safely (wire type is Record<string, unknown> | null).
// Returns the plan string only when it is a non-empty string, else null.
function planOf(call: Call): string | null {
  const p = (call.input as { plan?: unknown } | null | undefined)?.plan;
  return typeof p === 'string' && p.length > 0 ? p : null;
}

// Structured-input presence guards for the #177 S3 edit/shell cards. Computed
// in the switch BEFORE constructing a card so an absent/malformed input returns
// null and falls through to the generic chip (Codex P1.2). A card that itself
// rendered null would still be a truthy element at ToolCallChip and bypass the
// generic chip, vanishing the tool.
function hasEditInput(c: Call): boolean {
  const i = c.input as { old_string?: unknown; new_string?: unknown } | null | undefined;
  return !!i && typeof i.old_string === 'string' && typeof i.new_string === 'string';
}
function hasMultiEditInput(c: Call): boolean {
  // Require a NON-EMPTY edits[] — a zero-edit MultiEdit would render a hollow
  // card body, so fall through to the generic chip instead.
  const edits = (c.input as { edits?: unknown } | null | undefined)?.edits;
  return Array.isArray(edits) && edits.length > 0;
}
function hasWriteInput(c: Call): boolean {
  return typeof (c.input as { content?: unknown } | null | undefined)?.content === 'string';
}
function hasBashInput(c: Call): boolean {
  return typeof (c.input as { command?: unknown } | null | undefined)?.command === 'string';
}
// #177 S4 — web-card presence guards. Same Codex P1.2 rule: an absent/malformed
// input returns null and falls through to the generic chip instead of vanishing.
function hasWebFetchInput(c: Call): boolean {
  return typeof (c.input as { url?: unknown } | null | undefined)?.url === 'string';
}
function hasWebSearchInput(c: Call): boolean {
  return typeof (c.input as { query?: unknown } | null | undefined)?.query === 'string';
}
// Codex (mcp__codex__codex / -reply). Guard on a usable prompt (Codex P1.2);
// does NOT require a result — a request-only/active call has result:null and
// must still render the card.
function hasCodexInput(c: Call): boolean {
  return typeof (c.input as { prompt?: unknown } | null | undefined)?.prompt === 'string';
}

// Name-keyed dispatch to a dedicated renderer. Returns null when the tool has
// no special card (→ the generic chip). This is the extension point Sessions
// 3–4 reuse (Edit-diff, Bash-terminal, MCP).
export function specialToolRenderer(call: Call): ReactElement | null {
  switch ((call.name ?? '').toLowerCase()) {
    case 'askuserquestion': return <AskUserQuestionCard call={call} />;
    case 'todowrite': return <TodoWriteCard call={call} />;
    // Empty/missing plan → fall through to the generic chip (defensive, spec §4.7).
    case 'exitplanmode': return planOf(call) != null ? <ExitPlanModeCard call={call} /> : null;
    // #177 S3 — edit/shell cards. The guard runs HERE (before the element) so a
    // null/malformed input falls through to the generic chip instead of vanishing.
    case 'edit': return hasEditInput(call) ? <DiffCard call={call} /> : null;
    case 'multiedit': return hasMultiEditInput(call) ? <DiffCard call={call} /> : null;
    case 'write': return hasWriteInput(call) ? <DiffCard call={call} /> : null;
    case 'bash': return hasBashInput(call) ? <BashCard call={call} /> : null;
    // #177 S4 — web tools (Q6-A). Guard runs here so absent input falls
    // through to the generic chip instead of vanishing the tool.
    case 'webfetch': return hasWebFetchInput(call) ? <WebFetchCard call={call} /> : null;
    case 'websearch': return hasWebSearchInput(call) ? <WebSearchCard call={call} /> : null;
    // Codex MCP — dedicated card. Guard on a usable prompt; result may be null
    // (request-only/active call) and must still render the card.
    case 'mcp__codex__codex':
    case 'mcp__codex__codex-reply':
      return hasCodexInput(call) ? <CodexCard call={call} /> : null;
    default: return null;
  }
}
