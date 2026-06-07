import { MessageItem } from './MessageItem';
import { fmt } from '../lib/fmt';
import type { ConversationItem } from '../types/conversation';

const LABEL_MAX = 60;

// First non-blank line of the subagent's task prompt (its root message text),
// trimmed + truncated; falls back to the subagent hash when the root has no
// prose. Exported for unit testing.
export function subagentSummaryLabel(items: ConversationItem[], subagentKey: string): string {
  const text = items[0]?.text ?? '';
  const firstLine = text.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? '';
  if (!firstLine) return `Subagent ${subagentKey}`;
  return firstLine.length > LABEL_MAX ? `${firstLine.slice(0, LABEL_MAX).trimEnd()}…` : firstLine;
}

// One subagent thread (one agent-*.jsonl file) as a collapsed disclosure
// (#155). Summary = task-prompt line + message count + summed thread cost.
// `nested` adds an indent class when the group hangs under a parent main item.
// Members get no refs — a jump landing inside a collapsed thread no-ops (v1).
export function SidechainGroup({
  subagentKey,
  items,
  nested,
}: {
  subagentKey: string;
  items: ConversationItem[];
  nested: boolean;
}) {
  const label = subagentSummaryLabel(items, subagentKey);
  const cost = items.reduce((acc, it) => acc + (it.cost_usd ?? 0), 0);
  return (
    <details className={nested ? 'conv-sidechain conv-sidechain--nested' : 'conv-sidechain'}>
      <summary>
        🧵 {label} · {items.length} msgs · <span className="conv-sidechain-cost">{fmt.usd2(cost)}</span>
      </summary>
      <div className="conv-sidechain-body">
        {items.map((item) => (
          <MessageItem key={item.anchor.uuid} item={item} />
        ))}
      </div>
    </details>
  );
}
