import { useEffect, useState } from 'react';
import { MessageItem } from './MessageItem';
import { SubagentIcon } from './ConvIcons';
import { fmt } from '../lib/fmt';
import type { ConversationItem, SubagentMeta } from '../types/conversation';

const LABEL_MAX = 60;

// #166: subagent result status badge. Always a bare ✓ on the happy path (the
// word "completed" would be noise); the word is spelled out only on failure
// (✕ error) or any other non-completed terminal state (⚠ <status>). null when
// the result carried no status field.
function statusBadge(status?: string) {
  if (status == null) return null;
  if (status === 'completed')
    return <span className="conv-subagent-ok" aria-label="completed" title="completed">✓</span>;
  if (status === 'error')
    return <span className="conv-subagent-err"><span aria-hidden="true">✕</span> error</span>;
  return <span className="conv-subagent-warn"><span aria-hidden="true">⚠</span> {status}</span>;
}

// First non-blank line of the subagent's task prompt (its root message text),
// trimmed + truncated; falls back to the subagent hash when the root has no
// prose. Exported for unit testing.
export function subagentSummaryLabel(items: ConversationItem[], subagentKey: string): string {
  // First NON-meta item, not items[0]: a subagent file can open with an
  // injected `meta` row (skill body / SessionStart injection) whose text would
  // otherwise leak "Base directory for this skill…" as the card title (Codex
  // P1.3). Fall back to items[0] if every item is meta.
  const root = items.find((it) => it.kind !== 'meta') ?? items[0];
  const text = root?.text ?? '';
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
  meta,
  getItemRef,
  rootUuid,
  getCardRef,
  forceOpen = false,
  riseClassName = '',
  riseStyle,
}: {
  subagentKey: string;
  items: ConversationItem[];
  nested: boolean;
  // #166: the subagent's kind + result meta, keyed off subagent_key by the
  // reader. Absent on old transcripts → the card degrades to title-only.
  meta?: SubagentMeta;
  getItemRef?: (item: ConversationItem) => (el: HTMLDivElement | null) => void;
  // #188 S3/B6 — the bucket-root uuid (= items[0].anchor.uuid, the same value
  // the outline subagent entry jumps to). It tags the <details> via data-uuid
  // and keys the card in the reader's cardRefs map.
  rootUuid?: string;
  // #188 S3/B6 — a stable ref-callback factory (per rootUuid) that registers the
  // <details> element in the reader's cardRefs map. Registered UNCONDITIONALLY
  // (open and closed), separate from getItemRef (inner-member refs), so a
  // collapsed subagent outline click resolves the CARD and flashes it without a
  // force-open (Bug 1). No key collision with itemRefs; no open/close race.
  getCardRef?: (rootUuid: string) => (el: HTMLElement | null) => void;
  forceOpen?: boolean;
  // G1 §4b load-in: the reader's render-time classifier passes `conv-rise`
  // (+ a per-index animationDelay) for a first-appearance top-level thread,
  // or '' to suppress (already seen, or the active jump target).
  riseClassName?: string;
  riseStyle?: React.CSSProperties;
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
  // `in` narrows: the meta arm has neither cost_usd nor model (injected content
  // carries no turn cost / model), so guard the access instead of summing/listing
  // phantom fields.
  const cost = items.reduce((acc, it) => acc + ('cost_usd' in it ? (it.cost_usd ?? 0) : 0), 0);
  const models = [...new Set(items.map((it) => ('model' in it ? it.model : null)).filter(Boolean))] as string[];
  return (
    <details
      // #188 S3/B6 — data-uuid = the bucket-root uuid (the outline subagent
      // entry's jump anchor); ref registers the card in cardRefs UNCONDITIONALLY
      // (whether collapsed or open) so a collapsed outline click flashes THIS
      // card, not an inner member.
      data-uuid={rootUuid}
      ref={rootUuid != null && getCardRef ? getCardRef(rootUuid) : undefined}
      className={[
        nested ? 'conv-sidechain conv-sidechain--nested' : 'conv-sidechain',
        // G1 §4a: while a #160 jump force-opens this thread, snap it open
        // instantly (CSS `transition: none`) so layout is final before
        // scrollIntoView lands. The class drops when the force releases, so
        // later user toggles animate.
        forceOpen ? 'conv-sidechain--force' : '',
        riseClassName,
      ].filter(Boolean).join(' ')}
      style={riseStyle}
      open={open}
      onToggle={(e) => setUserOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="conv-sidechain-head">
        <span className="conv-sidechain-glyph" aria-hidden="true"><SubagentIcon /></span>
        <span className="conv-sidechain-headtext">
          <span className="conv-sidechain-kind">
            Subagent{meta?.kind ? <span className="conv-sidechain-kindname"> · {meta.kind}</span> : null}
          </span>
          <span className="conv-sidechain-title">{label}</span>
          {meta && (meta.total_tokens != null || meta.total_duration_ms != null
                    || meta.total_tool_use_count != null || meta.status != null) && (
            <span className="conv-sidechain-submeta">
              {meta.total_tokens != null && <span>{fmt.compact(meta.total_tokens)} tok</span>}
              {meta.total_duration_ms != null && <span>{fmt.durationMs(meta.total_duration_ms)}</span>}
              {meta.total_tool_use_count != null && (
                <span>{meta.total_tool_use_count} {meta.total_tool_use_count === 1 ? 'tool' : 'tools'}</span>
              )}
              {statusBadge(meta.status)}
            </span>
          )}
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
