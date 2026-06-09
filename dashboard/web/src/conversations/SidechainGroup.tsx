import { useEffect, useState } from 'react';
import { MessageItem } from './MessageItem';
import { SubagentIcon } from './ConvIcons';
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

// One subagent thread (one agent-*.jsonl file) as a disclosure (#155). Summary =
// task-prompt line + message count + summed thread cost. `nested` adds an indent
// class when the group hangs under a parent main item.
//
// Jump-to-message support (#160): the reader force-opens the owning thread when a
// jump targets a collapsed member. `open` is DERIVED (`userOpen || forceOpen`) so a
// force opens the group in the SAME render — the target member's ref then attaches
// in that commit and the reader's jump effect can scroll to it. Members get a ref
// ONLY while open: a collapsed <details> hides them (scrollIntoView on a hidden node
// no-ops), and the ref-less state is exactly what tells the reader to force-open.
// The latch effect pins `userOpen` true on a force so the thread stays expanded —
// and manually collapsible — after the reader clears its force-key.
export function SidechainGroup({
  subagentKey,
  items,
  nested,
  getItemRef,
  forceOpen = false,
}: {
  subagentKey: string;
  items: ConversationItem[];
  nested: boolean;
  getItemRef?: (item: ConversationItem) => (el: HTMLDivElement | null) => void;
  forceOpen?: boolean;
}) {
  const [userOpen, setUserOpen] = useState(false);
  const open = userOpen || forceOpen;
  // Latch a force into userOpen so the thread stays open after forceOpen drops
  // (independent of whether the browser/jsdom fires `toggle` on a programmatic
  // `open` change). `open` is already true via the derivation this same render,
  // so this causes no flicker and the member ref was already attached.
  useEffect(() => {
    if (forceOpen) setUserOpen(true);
  }, [forceOpen]);

  const label = subagentSummaryLabel(items, subagentKey);
  const cost = items.reduce((acc, it) => acc + (it.cost_usd ?? 0), 0);
  const models = [...new Set(items.map((it) => it.model).filter(Boolean))] as string[];
  return (
    <details
      className={nested ? 'conv-sidechain conv-sidechain--nested' : 'conv-sidechain'}
      open={open}
      onToggle={(e) => setUserOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="conv-sidechain-head">
        <span className="conv-sidechain-glyph" aria-hidden="true"><SubagentIcon /></span>
        <span className="conv-sidechain-headtext">
          <span className="conv-sidechain-kind">Subagent</span>
          <span className="conv-sidechain-title">{label}</span>
        </span>
        <span className="conv-sidechain-meta">
          {models.length > 0 && <span className="conv-sidechain-model">{models.join(', ')}</span>}
          <span>{items.length} msgs</span>
          <span className="conv-sidechain-cost">{fmt.usd2(cost)}</span>
          <span className="conv-chev" aria-hidden="true" />
        </span>
      </summary>
      <div className="conv-sidechain-body">
        {items.map((item) => (
          <MessageItem
            key={item.anchor.uuid}
            item={item}
            // Relies on getItemRef returning a STABLE callback per item (the
            // reader memoizes them in refCallbacks): the value is identical
            // across renders while open, so React doesn't detach/reattach and
            // MessageItem's memo isn't thrashed. Toggling open swaps it to/from
            // undefined, which is the intended detach/attach.
            ref={open && getItemRef ? getItemRef(item) : undefined}
          />
        ))}
      </div>
    </details>
  );
}
