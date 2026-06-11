import type { ReactElement } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { AskUserQuestionCard } from './AskUserQuestionCard';
import { TodoWriteCard } from './TodoWriteCard';
import { ExitPlanModeCard } from './ExitPlanModeCard';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// Name-keyed dispatch to a dedicated renderer. Returns null when the tool has
// no special card (→ the generic chip). This is the extension point Sessions
// 3–4 reuse (Edit-diff, Bash-terminal, MCP).
export function specialToolRenderer(call: Call): ReactElement | null {
  switch ((call.name ?? '').toLowerCase()) {
    case 'askuserquestion': return <AskUserQuestionCard call={call} />;
    case 'todowrite': return <TodoWriteCard call={call} />;
    case 'exitplanmode': return <ExitPlanModeCard call={call} />;
    default: return null;
  }
}
