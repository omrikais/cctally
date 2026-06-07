import { forwardRef, memo } from 'react';
import { Markdown } from '../components/Markdown';
import { MessageBlocks } from './MessageBlocks';
import type { ConversationItem } from '../types/conversation';

// A single reader message. forwardRef exposes the container div so the
// reader can scrollIntoView on a jump. Human/assistant render their joined
// prose via <Markdown> plus any non-text blocks via <MessageBlocks>; the
// assistant turn additionally shows a model badge and renders its per-turn
// cost EXACTLY ONCE (the backend already counts a turn's cost once — see
// the cost_usd contract). A top-level tool_result item (empty prose)
// collapses into a single disclosure wrapping its blocks. Memoized for long
// transcripts.
function MessageItemImpl(
  { item }: { item: ConversationItem },
  ref: React.ForwardedRef<HTMLDivElement>,
) {
  // tool_result top-level kind: empty prose, render as a collapsed
  // disclosure wrapping the blocks.
  if (item.kind === 'tool_result') {
    return (
      <div ref={ref} className="conv-item conv-item--tool_result" data-uuid={item.anchor.uuid}>
        <details className="conv-chip conv-chip--result">
          <summary>📤 Tool result</summary>
          <div className="conv-chip-body"><MessageBlocks blocks={item.blocks} /></div>
        </details>
      </div>
    );
  }

  if (item.kind === 'assistant') {
    // `> 0`, not just `typeof === 'number'`: the backend's _build_simple emits
    // an explicit cost_usd of 0.0 for an assistant-with-null-msg_id row (and a
    // real turn with no session_entries match also rounds to 0.0). Those are
    // "no attributable cost" sentinels, not a genuine $0.0000 charge — showing
    // the footer for them is misleading, so only render it for a positive cost.
    const hasCost = typeof item.cost_usd === 'number' && item.cost_usd > 0;
    return (
      <div ref={ref} className="conv-item conv-item--assistant" data-uuid={item.anchor.uuid}>
        <div className="conv-item-head">
          <span className="conv-item-label">Assistant</span>
          <span className="conv-item-model">{item.model ?? '—'}</span>
        </div>
        {item.text && <Markdown>{item.text}</Markdown>}
        <MessageBlocks blocks={item.blocks} />
        {hasCost && (
          // toFixed(4), not fmt.usd2: per-turn costs are typically sub-cent,
          // where 2-decimal formatting would read "$0.00" — 4 decimals keep
          // the real figure legible. Intentional bypass of the usd2 helper.
          <div className="conv-item-cost">${(item.cost_usd as number).toFixed(4)}</div>
        )}
      </div>
    );
  }

  // human
  return (
    <div ref={ref} className="conv-item conv-item--human" data-uuid={item.anchor.uuid}>
      <div className="conv-item-head">
        <span className="conv-item-label">You</span>
      </div>
      {item.text && <Markdown>{item.text}</Markdown>}
      <MessageBlocks blocks={item.blocks} />
    </div>
  );
}

export const MessageItem = memo(forwardRef<HTMLDivElement, { item: ConversationItem }>(MessageItemImpl));
