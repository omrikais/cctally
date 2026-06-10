import { forwardRef, memo } from 'react';
import { Markdown } from '../components/Markdown';
import { MessageBlocks } from './MessageBlocks';
import { ResultIcon, SystemIcon, SkillIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { PermalinkButton } from './PermalinkButton';
import { isSystemMarker } from './systemMarkers';
import type { ConversationItem } from '../types/conversation';

// First non-blank line of a meta body, trimmed + capped — the context pill's
// collapsed one-line preview (skill/command pills don't need it).
function metaPreview(s: string): string {
  const t = s.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? '';
  return t.length > 80 ? `${t.slice(0, 80).trimEnd()}…` : t;
}

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
  { item, className = '', style }: { item: ConversationItem; className?: string; style?: React.CSSProperties },
  ref: React.ForwardedRef<HTMLDivElement>,
) {
  // Optional extra class/style (e.g. the G1 load-in `conv-rise` + per-index
  // animationDelay) is merged onto the root `.conv-item` div so it stays a
  // DIRECT child of the thread — the role-dot spine CSS keys on
  // `.conv-reader-thread > .conv-item--human`, so wrapping is not an option.
  const cls = (suffix: string) => `conv-item ${suffix}${className ? ` ${className}` : ''}`;
  // tool_result top-level kind: empty prose, render as a collapsed
  // disclosure wrapping the blocks.
  if (item.kind === 'tool_result') {
    return (
      <div ref={ref} className={cls('conv-item--tool_result')} style={style} data-uuid={item.anchor.uuid}>
        <details className="conv-chip conv-chip--result">
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            <ResultIcon /> Tool result
          </summary>
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
      <div ref={ref} className={cls('conv-item--assistant')} style={style} data-uuid={item.anchor.uuid}>
        <div className="conv-item-head">
          <span className="conv-item-label">Assistant</span>
          <span className="conv-item-model">{item.model ?? '—'}</span>
        </div>
        {/* Document-order walk renders prose (from text blocks) + thinking +
            tool runs in order — no separate item.text render (#164). */}
        <MessageBlocks blocks={item.blocks} />
        {item.text && (
          // Hover/focus-revealed action copying the turn's joined prose. Only
          // when there IS prose — a tool-only assistant turn renders none.
          <div className="conv-item-actions">
            <PermalinkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} />
            <CopyButton text={item.text} />
          </div>
        )}
        {hasCost && (
          // toFixed(4), not fmt.usd2: per-turn costs are typically sub-cent,
          // where 2-decimal formatting would read "$0.00" — 4 decimals keep
          // the real figure legible. Intentional bypass of the usd2 helper.
          <div className="conv-item-cost">${(item.cost_usd as number).toFixed(4)}</div>
        )}
      </div>
    );
  }

  // meta: injected harness content (skill bodies, slash-command plumbing,
  // git-context / "Continue…" / placeholders) — NEVER a "You" prompt. A
  // collapsed-by-default disclosure with a skill/system/context chrome; the
  // body renders via <MessageBlocks> so any non-text injected block survives
  // (Codex P1.1), except `command` which keeps the raw <pre> (its <command-*>
  // plumbing must not be markdown-mangled). No spine role-dot: the CSS only
  // targets --human/--assistant.
  if (item.kind === 'meta') {
    const mk = item.meta_kind;
    const head =
      mk === 'skill' ? (
        <>
          <SkillIcon /> <span className="conv-meta-label">Skill content</span>
          {item.skill_name && <span className="conv-meta-name">· {item.skill_name}</span>}
        </>
      ) : mk === 'command' ? (
        <>
          <SystemIcon /> <span className="conv-meta-label">System marker</span>
        </>
      ) : (
        <>
          <SystemIcon /> <span className="conv-meta-label">Injected context</span>
          <span className="conv-meta-preview">{metaPreview(item.text)}</span>
        </>
      );
    return (
      <div ref={ref} className={cls('conv-item--meta')} style={style} data-uuid={item.anchor.uuid}>
        <details className={`conv-meta conv-meta--${mk}`}>
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            {head}
          </summary>
          {mk === 'command' ? (
            <pre className="conv-meta-body conv-meta-body--pre">{item.text}</pre>
          ) : (
            <div className="conv-meta-body">
              <MessageBlocks blocks={item.blocks} />
              {mk === 'skill' && item.text && (
                <div className="conv-item-actions">
                  <PermalinkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} />
                  <CopyButton text={item.text} />
                </div>
              )}
            </div>
          )}
        </details>
      </div>
    );
  }

  // human: fold a pure system-marker turn (slash-command plumbing) into a
  // compact expandable pill. Guard on NO non-text blocks (the prose also
  // arrives as a {kind:'text'} block, so length===0 would never hold). The
  // raw text is never destroyed — expanding the <details> restores it.
  // (For isMeta lines this is now handled by the meta branch above; this
  // fallback still catches any non-isMeta legacy marker line.)
  if (isSystemMarker(item.text) && item.blocks.every((b) => b.kind === 'text')) {
    return (
      <div ref={ref} className={cls('conv-item--system')} style={style} data-uuid={item.anchor.uuid}>
        <details className="conv-system-marker">
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            <SystemIcon /> System marker
          </summary>
          <pre className="conv-system-marker-body">{item.text}</pre>
        </details>
      </div>
    );
  }

  return (
    <div ref={ref} className={cls('conv-item--human')} style={style} data-uuid={item.anchor.uuid}>
      <div className="conv-item-head">
        <span className="conv-item-label">You</span>
      </div>
      {item.text && <Markdown>{item.text}</Markdown>}
      {/* Joined prose renders above via item.text; pass only NON-text blocks to
          the walk so it doesn't double the human's prose. */}
      <MessageBlocks blocks={item.blocks.filter((b) => b.kind !== 'text')} />
      {item.text && (
        <div className="conv-item-actions">
          <PermalinkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} />
          <CopyButton text={item.text} />
        </div>
      )}
    </div>
  );
}

export const MessageItem = memo(
  forwardRef<HTMLDivElement, { item: ConversationItem; className?: string; style?: React.CSSProperties }>(
    MessageItemImpl,
  ),
);
