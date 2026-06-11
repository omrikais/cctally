import type { ChecklistTodo, ConversationBlock } from '../types/conversation';
import { ChecklistCard } from './ChecklistCard';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// Legacy adapter: the retired TodoWrite tool carried its checklist inline on
// the tool_call input ({ todos: [...] }). Pull it out and hand it to the shared
// ChecklistCard. Kept for historical transcripts; the live to-do mechanism is
// the Task* family (see TaskChecklistCard).
function todosOf(call: Call): ChecklistTodo[] {
  const t = (call.input as { todos?: unknown } | null | undefined)?.todos;
  return Array.isArray(t) ? (t as ChecklistTodo[]) : [];
}

export function TodoWriteCard({ call }: { call: Call }) {
  return <ChecklistCard todos={todosOf(call)} label="TodoWrite" />;
}
