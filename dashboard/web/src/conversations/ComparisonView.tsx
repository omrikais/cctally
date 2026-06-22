import { useMemo, useState } from 'react';
import { dispatch } from '../store/store';
import { useConversationOutline } from '../hooks/useConversationOutline';
import { useConversationPrompts } from '../hooks/useConversationPrompts';
import { useIsWide } from '../hooks/useIsWide';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt } from '../lib/fmt';
import { computeSequenceDiff, type SpinePrompt } from './sessionAlign';
import { metricsFromOutline } from './comparisonMetricsCalc';
import { ComparisonHeader, type SideHeader } from './ComparisonHeader';
import { ComparisonMetrics } from './ComparisonMetrics';
import { ComparisonDiff } from './ComparisonDiff';
import type { ConversationOutline } from '../types/conversation';

// #217 S7 F10 — the comparison view. Instantiates TWO useConversationOutline
// hooks (the prompt spine + metrics) and manages its own local transient state:
// the expanded row key + the lazy /prompts text caches. It dispatches
// SWAP_COMPARE / CLOSE_COMPARE; the rail's pick-mode drives entry. No live-tail
// (a static, re-openable snapshot of two finished runs) — it never mounts
// useConversation (the per-conversation EventSource owner).

// The main-thread human prompt spine, client-side from the outline turns — the
// SAME predicate as the /prompts route + sessionAlign (kind==='human' &&
// subagent_key==null && !is_sidechain + a non-empty label).
export function spineFromOutline(outline: ConversationOutline | null): SpinePrompt[] {
  if (!outline) return [];
  return outline.turns
    .filter(
      (t) =>
        t.kind === 'human' &&
        t.subagent_key == null &&
        !t.is_sidechain &&
        (t.label ?? '').trim() !== '',
    )
    .map((t) => ({ uuid: t.uuid, label: t.label }));
}

interface HeaderCtx { tz: string; offsetLabel: string }

function headerOf(
  sessionId: string,
  outline: ConversationOutline | null,
  ctx: HeaderCtx,
): SideHeader {
  // The outline carries no title — fall back to a short session id (the rail
  // ConversationSummary.title would be preferred when to hand, but the view
  // doesn't fetch the browse list). Date from the first turn's ts; model from
  // the stats.models keys.
  const date = outline?.turns[0]?.ts ? fmt.dateShort(outline.turns[0].ts, ctx) : null;
  const models = Object.keys(outline?.stats.models ?? {});
  return {
    title: `Session ${sessionId.slice(0, 8)}`,
    date: date ?? null,
    model: models.length ? models.join(', ') : null,
  };
}

export function ComparisonView({ a, b }: { a: string; b: string }) {
  const outA = useConversationOutline(a);
  const outB = useConversationOutline(b);
  const wide = useIsWide();
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };

  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  // Lazily load full prompt text once ANY row is expanded (per side). Both
  // hooks gate on the same `expandedKey !== null` flag so neither fetches until
  // the user actually expands a row.
  const promptsA = useConversationPrompts(a, expandedKey !== null);
  const promptsB = useConversationPrompts(b, expandedKey !== null);

  const spineA = useMemo(() => spineFromOutline(outA.outline), [outA.outline]);
  const spineB = useMemo(() => spineFromOutline(outB.outline), [outB.outline]);
  const rows = useMemo(() => computeSequenceDiff(spineA, spineB), [spineA, spineB]);

  if (outA.error || outB.error) {
    return <ComparisonNotFound onClose={() => dispatch({ type: 'CLOSE_COMPARE' })} />;
  }

  const mA = outA.outline ? metricsFromOutline(outA.outline, spineA.length) : null;
  const mB = outB.outline ? metricsFromOutline(outB.outline, spineB.length) : null;

  return (
    <div className={`conv-cmp ${wide ? 'conv-cmp--wide' : 'conv-cmp--unified'}`}>
      <ComparisonHeader
        a={headerOf(a, outA.outline, ctx)}
        b={headerOf(b, outB.outline, ctx)}
        onSwap={() => dispatch({ type: 'SWAP_COMPARE' })}
        onClose={() => dispatch({ type: 'CLOSE_COMPARE' })}
      />
      {mA && mB && <ComparisonMetrics a={mA} b={mB} />}
      <ComparisonDiff
        rows={rows}
        wide={wide}
        expandedKey={expandedKey}
        onToggleRow={(k) => setExpandedKey((cur) => (cur === k ? null : k))}
        promptsA={promptsA.byUuid ?? {}}
        promptsB={promptsB.byUuid ?? {}}
        onOpenInReader={(side, uuid) => {
          // OPEN_CONVERSATION clears `compare` (reverse-clear, Task 3) so the
          // single reader replaces the comparison, landing on the jumped turn.
          const sid = side === 'a' ? a : b;
          dispatch({
            type: 'OPEN_CONVERSATION',
            sessionId: sid,
            jump: { session_id: sid, uuid },
          });
        }}
      />
    </div>
  );
}

function ComparisonNotFound({ onClose }: { onClose: () => void }) {
  return (
    <div className="conv-cmp conv-cmp--notfound">
      <div className="conv-cmp-notfound-msg">
        Couldn't load one of these sessions — it may have been removed.
      </div>
      <button type="button" className="conv-cmp-close" aria-label="Close comparison" onClick={onClose}>
        ✕ Close comparison
      </button>
    </div>
  );
}
