import { useCallback, useMemo, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useConversationOutline } from '../hooks/useConversationOutline';
import { useConversationPrompts } from '../hooks/useConversationPrompts';
import { useIsWide } from '../hooks/useIsWide';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt } from '../lib/fmt';
import { conversationEntityUrl } from '../lib/conversationTransport';
import { computeSequenceDiff, type SpinePrompt } from './sessionAlign';
import { metricsFromOutline } from './comparisonMetricsCalc';
import { ComparisonHeader, type SideHeader } from './ComparisonHeader';
import { ComparisonMetrics } from './ComparisonMetrics';
import { ComparisonLegend } from './ComparisonLegend';
import { ComparisonDiff } from './ComparisonDiff';
import { useCopy } from './useCopy';
import { conversationRefKey, normalizeConversationRef, type ConversationOutline, type ConversationRef, type ConversationRefInput } from '../types/conversation';

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
        // #227 — basis note: the outline carries only the ANSI-stripped `label`
        // (first non-blank rendered line), whereas the /prompts route keeps a
        // turn on the non-stripped `_item_text`. A human prompt whose ENTIRE
        // text is ANSI/control chars would be kept by /prompts but dropped here,
        // diverging the Prompts count + alignment rows. That input is
        // unreachable for real human prompts (a human prompt always carries
        // visible text), so the spine stays on the stripped `label` the outline
        // already ships rather than refetching raw text just to match the edge.
        (t.label ?? '').trim() !== '',
    )
    .map((t) => ({ uuid: t.uuid, label: t.label }));
}

interface HeaderCtx { tz: string; offsetLabel: string }

function headerOf(
  conversationRef: ConversationRef,
  outline: ConversationOutline | null,
  ctx: HeaderCtx,
  titles: Record<string, string>,
  legacyTitleKey?: string,
): SideHeader {
  // #227 — prefer the real derived title from the shared rail title cache (the
  // spec's "title from the rail conversations list if loaded"); fall back to a
  // short session id when the rail hasn't loaded it (e.g. a cold-boot compare
  // from a pasted URL, or the mobile flow where the rail is hidden). The outline
  // itself carries no title. Date from the first turn's ts; model from the
  // stats.models keys.
  const cachedTitle = titles[conversationRefKey(conversationRef)]?.trim()
    ?? (legacyTitleKey ? titles[legacyTitleKey]?.trim() : undefined);
  const date = outline?.turns[0]?.ts ? fmt.dateShort(outline.turns[0].ts, ctx) : null;
  const models = Object.keys(outline?.stats.models ?? {});
  return {
    title: cachedTitle ? cachedTitle : `Session ${conversationRef.key.slice(0, 8)}`,
    date: date ?? null,
    model: models.length ? models.join(', ') : null,
    source: conversationRef.source,
  };
}

export function composeComparisonExport(
  a: ConversationRef,
  aTitle: string,
  aBody: string,
  b: ConversationRef,
  bTitle: string,
  bBody: string,
): string {
  const section = (label: 'A' | 'B', ref: ConversationRef, title: string, body: string) =>
    `## Run ${label} · ${ref.source === 'codex' ? 'Codex' : 'Claude'} · ${title}\n\n${body.trim()}`;
  return `# Conversation comparison\n\n${section('A', a, aTitle, aBody)}\n\n---\n\n${section('B', b, bTitle, bBody)}\n`;
}

export function ComparisonView({ a: rawA, b: rawB }: { a: ConversationRefInput; b: ConversationRefInput }) {
  const a = normalizeConversationRef(rawA);
  const b = normalizeConversationRef(rawB);
  // #227 — static snapshot of two finished runs (no live-tail by design), so the
  // outline hooks skip per-SSE-tick revalidation: the comparison opens once and
  // stays put instead of re-fetching both /outline endpoints ~30+ times a minute
  // while a compared session live-tails elsewhere.
  const outA = useConversationOutline(a, { revalidateOnTick: false });
  const outB = useConversationOutline(b, { revalidateOnTick: false });
  const wide = useIsWide();
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  // #227 — the shared rail title cache (populated by useConversations); lets the
  // header show the real derived title without fetching the browse list here.
  const titles = useSyncExternalStore(subscribeStore, () => getState().conversationTitles);
  const { copy } = useCopy();
  const [exportBusy, setExportBusy] = useState(false);

  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  // Lazily load full prompt text once ANY row is expanded (per side). Both
  // hooks gate on the same `expandedKey !== null` flag so neither fetches until
  // the user actually expands a row.
  const promptsA = useConversationPrompts(a, expandedKey !== null);
  const promptsB = useConversationPrompts(b, expandedKey !== null);

  const spineA = useMemo(() => spineFromOutline(outA.outline), [outA.outline]);
  const spineB = useMemo(() => spineFromOutline(outB.outline), [outB.outline]);
  const rows = useMemo(() => computeSequenceDiff(spineA, spineB), [spineA, spineB]);

  // #228 S1 (F3) — one shared close handler for BOTH the header and the
  // not-found state, so every way out of the comparison goes through
  // CLOSE_COMPARE (which arms the reader's focus-return to #conv-compare-with).
  const onClose = () => dispatch({ type: 'CLOSE_COMPARE' });

  const mA = outA.outline ? metricsFromOutline(outA.outline, spineA.length) : null;
  const mB = outB.outline ? metricsFromOutline(outB.outline, spineB.length) : null;

  // #228 S5 E2 — resolve both side headers once and reuse the titles for the
  // sticky wide-mode column header (ComparisonDiff) so identity is anchored.
  const ha = headerOf(a, outA.outline, ctx, titles, typeof rawA === 'string' ? rawA : undefined);
  const hb = headerOf(b, outB.outline, ctx, titles, typeof rawB === 'string' ? rawB : undefined);
  const copyExport = useCallback(async () => {
    setExportBusy(true);
    try {
      const [ra, rb] = await Promise.all([
        fetch(conversationEntityUrl(a, 'export', { scope: 'all' })),
        fetch(conversationEntityUrl(b, 'export', { scope: 'all' })),
      ]);
      if (!ra.ok || !rb.ok) return;
      copy(composeComparisonExport(a, ha.title, await ra.text(), b, hb.title, await rb.text()));
    } finally {
      setExportBusy(false);
    }
  }, [a, b, ha.title, hb.title, copy]);

  if (outA.error || outB.error) {
    return <ComparisonNotFound onClose={onClose} />;
  }

  return (
    <div className={`conv-cmp ${wide ? 'conv-cmp--wide' : 'conv-cmp--unified'}`}>
      <ComparisonHeader
        a={ha}
        b={hb}
        onSwap={() => dispatch({ type: 'SWAP_COMPARE' })}
        onExport={() => void copyExport()}
        exportBusy={exportBusy}
        onClose={onClose}
      />
      {mA && mB && <ComparisonMetrics a={mA} b={mB} aSource={a.source} bSource={b.source} />}
      <ComparisonLegend wide={wide} />
      <ComparisonDiff
        rows={rows}
        wide={wide}
        aTitle={ha.title}
        bTitle={hb.title}
        expandedKey={expandedKey}
        onToggleRow={(k) => setExpandedKey((cur) => (cur === k ? null : k))}
        promptsA={promptsA.byUuid ?? {}}
        promptsB={promptsB.byUuid ?? {}}
        onOpenInReader={(side, uuid) => {
          // OPEN_CONVERSATION clears `compare` (reverse-clear, Task 3) so the
          // single reader replaces the comparison, landing on the jumped turn.
          const conversationRef = side === 'a' ? a : b;
          dispatch({
            type: 'OPEN_CONVERSATION',
            conversationRef,
            jump: { conversation_ref: conversationRef, session_id: conversationRef.key, uuid },
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
