import type { ConversationBlock } from '../types/conversation';
import { ChecklistCard } from './ChecklistCard';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// Live-tool adapter: Claude Code's Task* family (TaskCreate / TaskUpdate /
// TaskList) doesn't carry the whole list inline — the kernel reconstructs the
// running to-do list and stamps a task_snapshot onto the run's first tool_call.
// Hand that snapshot to the shared ChecklistCard with the "Tasks" label. An
// absent snapshot (legacy / non-folded run) degrades to an empty "no todos"
// card without crashing.
export function TaskChecklistCard({ call }: { call: Call }) {
  return <ChecklistCard todos={call.task_snapshot ?? []} label="Tasks" />;
}
