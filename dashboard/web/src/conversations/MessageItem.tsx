import { forwardRef, memo } from 'react';
import { Markdown } from '../components/Markdown';
import { MessageBlocks } from './MessageBlocks';
import { isSystemMarker } from './systemMarkers';
import type { ConversationItem } from '../types/conversation';

// A single reader message. forwardRef exposes the container div so the
// reader can scrollIntoView on a jump.
//
// The ASSISTANT turn renders its body as a single document-order block walk
// (#164): prose-from-text-blocks, thinking, and tool runs interleave in the
// order they happened, so the joined `item.text` is NOT rendered separately
// (that would double the prose). It additionally shows a model badge and
// renders its per-turn cost EXACTLY ONCE (the backend counts a turn's cost
// once — see the cost_usd contract).
//
// The HUMAN turn is unchanged: its joined prose renders via <Markdown> and only
// its NON-text blocks pass to <MessageBlocks> (text would double the prose the
// walk now renders). The system-marker fold keys on item.text and short-circuits
// before this path.
//
// A top-level tool_result item (empty prose) collapses into a single disclosure
// wrapping its blocks. Memoized for long transcripts.
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
        {/* Document-order walk renders prose (from text blocks) + thinking +
            tool runs in order — no separate item.text render (#164). */}
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

  // human: fold a pure system-marker turn (slash-command plumbing) into a
  // compact expandable pill. Guard on NO non-text blocks (the prose also
  // arrives as a {kind:'text'} block, so length===0 would never hold). The
  // raw text is never destroyed — expanding the <details> restores it.
  if (isSystemMarker(item.text) && item.blocks.every((b) => b.kind === 'text')) {
    return (
      <div ref={ref} className="conv-item conv-item--system" data-uuid={item.anchor.uuid}>
        <details className="conv-system-marker">
          <summary>⚙ System marker</summary>
          <pre className="conv-system-marker-body">{item.text}</pre>
        </details>
      </div>
    );
  }

  return (
    <div ref={ref} className="conv-item conv-item--human" data-uuid={item.anchor.uuid}>
      <div className="conv-item-head">
        <span className="conv-item-label">You</span>
      </div>
      {item.text && <Markdown>{item.text}</Markdown>}
      {/* Joined prose renders above via item.text; pass only NON-text blocks to
          the walk so it doesn't double the human's prose. */}
      <MessageBlocks blocks={item.blocks.filter((b) => b.kind !== 'text')} />
    </div>
  );
}

export const MessageItem = memo(forwardRef<HTMLDivElement, { item: ConversationItem }>(MessageItemImpl));
