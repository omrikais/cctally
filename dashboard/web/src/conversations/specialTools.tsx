import type { ReactElement } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { AskUserQuestionCard } from './AskUserQuestionCard';
import { TodoWriteCard } from './TodoWriteCard';
import { ExitPlanModeCard } from './ExitPlanModeCard';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// Read call.input?.plan safely (wire type is Record<string, unknown> | null).
// Returns the plan string only when it is a non-empty string, else null.
function planOf(call: Call): string | null {
  const p = (call.input as { plan?: unknown } | null | undefined)?.plan;
  return typeof p === 'string' && p.length > 0 ? p : null;
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
    default: return null;
  }
}
