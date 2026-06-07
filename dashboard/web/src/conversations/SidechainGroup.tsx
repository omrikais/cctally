import { MessageItem } from './MessageItem';
import type { ConversationItem } from '../types/conversation';

// Collapsed group wrapping a contiguous run of sidechain (subagent-thread)
// messages — the frontend-only v1 treatment from groupSidechains. Members
// render as plain <MessageItem>s; they don't receive refs (a jump landing
// inside a collapsed subagent thread is a rare v1 edge — the reader's scroll
// simply no-ops, per the plan).
export function SidechainGroup({ items }: { items: ConversationItem[] }) {
  return (
    <details className="conv-sidechain">
      <summary>🧵 Subagent thread · {items.length} messages</summary>
      <div className="conv-sidechain-body">
        {items.map((item) => (
          <MessageItem key={item.anchor.uuid} item={item} />
        ))}
      </div>
    </details>
  );
}
