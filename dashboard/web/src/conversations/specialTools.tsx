import type { ReactElement } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { AskUserQuestionCard } from './AskUserQuestionCard';
import { TodoWriteCard } from './TodoWriteCard';
import { ExitPlanModeCard } from './ExitPlanModeCard';
import { DiffCard } from './DiffCard';
import { BashCard } from './BashCard';

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
  return Array.isArray((c.input as { edits?: unknown } | null | undefined)?.edits);
}
function hasWriteInput(c: Call): boolean {
  return typeof (c.input as { content?: unknown } | null | undefined)?.content === 'string';
}
function hasBashInput(c: Call): boolean {
  return typeof (c.input as { command?: unknown } | null | undefined)?.command === 'string';
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
    default: return null;
  }
}
