import { forwardRef, memo } from 'react';
import { Markdown } from '../components/Markdown';
import { MessageBlocks } from './MessageBlocks';
import { ResultIcon, SystemIcon, SkillIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { PermalinkButton } from './PermalinkButton';
import { BookmarkButton } from './BookmarkButton';
import { isSystemMarker } from './systemMarkers';
import { modelChipClass } from '../lib/model';
import { fmt } from '../lib/fmt';
import { costIntensity } from '../lib/cost';
import { useFmtCtx, useMarkersEnabled, useMaxTurnCost } from './TranscriptContext';
import { segmentContextBody, parseUnifiedDiff } from './contextDiff';
import { UnifiedDiffView } from './UnifiedDiffView';
import type { ConversationItem } from '../types/conversation';

// #217 S5 F6 — an injected `meta_kind:'context'` body sometimes carries an
// UNFENCED git diff (e.g. `- Unstaged changes: diff --git a/CLAUDE.md …`). Split
// the body into prose + diff segments (conservative `diff --git` anchor) and
// render prose as Markdown (unchanged) and diff segments as a UnifiedDiffView.
// A body with no `diff --git` marker is a single prose segment → all Markdown,
// exactly as before.
function ContextBody({ text }: { text: string }) {
  const segments = segmentContextBody(text);
  return (
    <>
      {segments.map((seg, i) =>
        seg.kind === 'diff' ? (
          <UnifiedDiffView key={i} files={parseUnifiedDiff(seg.text)} />
        ) : (
          <Markdown key={i}>{seg.text}</Markdown>
        ),
      )}
    </>
  );
}

// First non-blank line of a meta body, trimmed + capped — the context pill's
// collapsed one-line preview (skill/command pills don't need it).
function metaPreview(s: string): string {
  const t = s.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? '';
  return t.length > 80 ? `${t.slice(0, 80).trimEnd()}…` : t;
}

// Pull the human <summary> line out of a <task|bash-notification> body for the
// collapsed pill preview; falls back to the generic first-line metaPreview.
function notificationSummary(s: string): string {
  const m = s.match(/<summary>([\s\S]*?)<\/summary>/);
  return m ? metaPreview(m[1]) : metaPreview(s);
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
  { item, className = '', style, suppressToolUseIds }: {
    item: ConversationItem;
    className?: string;
    style?: React.CSSProperties;
    // §5 — spawn tool_use_ids whose nested subagent card is canonical; forwarded
    // to every MessageBlocks render so a spawn chip is suppressed wherever it
    // sits (main item AND subagent-thread item — a grandchild's spawn lives in a
    // child thread). A stable Set identity (reader-memoized) keeps the memo valid.
    suppressToolUseIds?: Set<string>;
  },
  ref: React.ForwardedRef<HTMLDivElement>,
) {
  // Optional extra class/style (e.g. the G1 load-in `conv-rise` + per-index
  // animationDelay) is merged onto the root `.conv-item` div so it stays a
  // DIRECT child of the thread — the role-dot spine CSS keys on
  // `.conv-reader-thread > .conv-item--human`, so wrapping is not an option.
  const cls = (suffix: string) => `conv-item ${suffix}${className ? ` ${className}` : ''}`;
  // #177 S5 §6 — eyebrow time `· HH:mm` on every item kind's head/summary line.
  // Routed through fmt.timeHHmm with the display-tz context (the chokepoint
  // rule); `noSuffix` drops the per-row tz abbrev (the tooltip carries the full
  // precise timestamp). `ts` is nullable (Codex F6) — a null-ts item renders no
  // time span at all.
  // #184 — read the display-tz FmtCtx from TranscriptContext (the reader computes
  // it once and provides it), NOT a per-item useDisplayTz() subscription — the
  // latter would re-render every memoized item on each SSE tick.
  const fmtCtx = useFmtCtx();
  // cache-failure-markers spec §3 — the opt-out, read once from the reader-
  // provided context (NOT a per-item store subscription; keeps memo valid).
  const markersEnabled = useMarkersEnabled();
  // fmt.timeHHmm returns the "—" sentinel for a null/unparseable ts; suppress the
  // eyebrow in that case (no real instant to show).
  const eyebrowTimeRaw = item.ts
    ? fmt.timeHHmm(item.ts, fmtCtx, { noSuffix: true })
    : null;
  const eyebrowTime = eyebrowTimeRaw && eyebrowTimeRaw !== '—' ? eyebrowTimeRaw : null;
  const eyebrow = eyebrowTime ? (
    <span className="conv-item-time" title={item.ts ?? undefined}>· {eyebrowTime}</span>
  ) : null;
  // tool_result top-level kind: empty prose, render as a collapsed
  // disclosure wrapping the blocks.
  if (item.kind === 'tool_result') {
    return (
      <div ref={ref} className={cls('conv-item--tool_result')} style={style} data-uuid={item.anchor.uuid}>
        <details className="conv-chip conv-chip--result">
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            <ResultIcon /> Tool result
            <PermalinkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} className="conv-chip-permalink" />
            {eyebrow}
          </summary>
          <div className="conv-chip-body"><MessageBlocks blocks={item.blocks} anchorUuid={item.anchor.uuid} suppressToolUseIds={suppressToolUseIds} /></div>
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
    // #177 S5 §6 — per-turn token usage, present only when the turn key matched a
    // session_entries row (absent → cost-only footer, the established
    // graceful-degradation pattern). Narrowed off the assistant kind.
    const tok = 'tokens' in item ? item.tokens : undefined;
    // cache-failure-markers spec §3 — the per-turn prompt-cache-failure marker.
    // Present only on a flagged turn; the chip renders iff markers are on AND
    // the turn is flagged. Pure prop derivation → memo stays valid.
    const cf = 'cache_failure' in item ? item.cache_failure : undefined;
    // #217 S6 F3 — per-turn cost micro-bar. The denominator is the session's
    // heaviest LOADED turn cost, provided once by the reader on the context (no
    // per-item store subscription). costIntensity returns the cost/max ratio
    // clamped to [0,1], or 0 when there is no positive denominator (→ no bar).
    const maxTurnCost = useMaxTurnCost();
    const costFrac = hasCost ? costIntensity(item.cost_usd as number, maxTurnCost) : 0;
    return (
      <div ref={ref} className={cls('conv-item--assistant')} style={style} data-uuid={item.anchor.uuid}>
        <div className="conv-item-head">
          <span className="conv-item-label">Assistant</span>
          {/* #175 F3: render the model through the shared .chip system (matching
              the rest of the dashboard). No chip — and no em dash — when null. */}
          {item.model && <span className={`chip ${modelChipClass(item.model)}`}>{item.model}</span>}
          {/* cache-failure-markers spec §3 — amber "cache rebuilt" chip next to
              the model chip. Gated on markersEnabled && the flag; the ⚡ glyph is
              aria-hidden so the chip text + aria-label/title carry the meaning. */}
          {markersEnabled && cf && (
            <span
              className="conv-cache-chip"
              aria-label={`Prompt cache miss: ${cf.tokens_recreated.toLocaleString()} tokens re-created instead of read from cache, about ${fmt.usd2(cf.est_wasted_usd)} extra`}
              title={`Cache rebuilt — ${fmt.compact(cf.tokens_recreated, { upper: true })} re-created instead of read (~${fmt.usd2(cf.est_wasted_usd)} extra). Usually follows an idle gap past the cache TTL.`}
            >
              <span aria-hidden="true">⚡</span> CACHE REBUILT · {fmt.compact(cf.tokens_recreated, { upper: true })} · +{fmt.usd2(cf.est_wasted_usd)}
            </span>
          )}
          {eyebrow}
        </div>
        {/* Document-order walk renders prose (from text blocks) + thinking +
            tool runs in order — no separate item.text render (#164). */}
        <MessageBlocks blocks={item.blocks} anchorUuid={item.anchor.uuid} suppressToolUseIds={suppressToolUseIds} />
        {item.text && (
          // Hover/focus-revealed action copying the turn's joined prose. Only
          // when there IS prose — a tool-only assistant turn renders none.
          <div className="conv-item-actions">
            <PermalinkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} />
            <CopyButton text={item.text} />
            <BookmarkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} />
          </div>
        )}
        {(hasCost || tok) && (
          // #177 S5 §6 — footer renders when `hasCost || tokens`: a zero-cost
          // turn that carries tokens shows a tokens-only footer; an un-reingested
          // turn without tokens keeps the cost-only footer. cache = creation +
          // read summed for display; the `title` breaks out the four exact counts.
          // toFixed(4), not fmt.usd2: per-turn costs are typically sub-cent,
          // where 2-decimal formatting would read "$0.00" — 4 decimals keep
          // the real figure legible. Intentional bypass of the usd2 helper.
          <div
            // cache-failure-markers spec §3 — tint the footer's `cache NNN`
            // figure amber on a flagged turn (only when markers are on, so the
            // opt-out hides this cue too).
            className={`conv-item-cost${markersEnabled && cf ? ' is-cache-failure' : ''}`}
            title={tok ? `input ${tok.input} · output ${tok.output} · cache create ${tok.cache_creation} · cache read ${tok.cache_read}` : undefined}
          >
            {hasCost && `$${(item.cost_usd as number).toFixed(4)}`}
            {tok && (
              <>
                {hasCost ? ' · ' : ''}in {fmt.tokens(tok.input)} · out {fmt.tokens(tok.output)} · cache {fmt.tokens(tok.cache_creation + tok.cache_read)}
              </>
            )}
            {/* #217 S6 F3 — relative cost micro-bar: width/intensity ∝
                cost / session max-turn-cost. Decorative (the exact $-figure above
                is the accessible value), so aria-hidden. Rendered only with a
                positive ratio (a costless turn / zero denominator → no bar). */}
            {costFrac > 0 && (
              <span
                className="conv-cost-bar"
                aria-hidden="true"
                style={{ ['--conv-cost-frac' as string]: String(costFrac) }}
              />
            )}
          </div>
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
      ) : mk === 'compaction' ? (
        <>
          <SystemIcon /> <span className="conv-meta-label">Compacted earlier conversation</span>
        </>
      ) : mk === 'notification' ? (
        <>
          <SystemIcon /> <span className="conv-meta-label">Background task</span>
          <span className="conv-meta-preview">{notificationSummary(item.text)}</span>
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
            {eyebrow}
          </summary>
          {mk === 'command' ? (
            <pre className="conv-meta-body conv-meta-body--pre">{item.text}</pre>
          ) : mk === 'context' ? (
            // #217 S5 F6 — render the injected context body as prose + any
            // unfenced git diff (ContextBody splits + routes diff segments
            // through UnifiedDiffView). No `diff --git` marker → all prose.
            <div className="conv-meta-body">
              <ContextBody text={item.text} />
            </div>
          ) : (
            <div className="conv-meta-body">
              <MessageBlocks blocks={item.blocks} anchorUuid={item.anchor.uuid} suppressToolUseIds={suppressToolUseIds} />
              {mk === 'skill' && item.text && (
                <div className="conv-item-actions">
                  <PermalinkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} />
                  <CopyButton text={item.text} />
                  <BookmarkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} />
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
            <PermalinkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} className="conv-chip-permalink" />
          </summary>
          <pre className="conv-system-marker-body">{item.text}</pre>
        </details>
      </div>
    );
  }

  // #188 — a promoted slash-command turn carries `command_name`; show it as a
  // compact badge next to "You". The args are item.text (rendered as prose
  // below); the raw <command-*> plumbing lives only in the lone text block,
  // which the non-text MessageBlocks walk below already filters out.
  const commandName = 'command_name' in item ? item.command_name : null;
  return (
    <div ref={ref} className={cls('conv-item--human')} style={style} data-uuid={item.anchor.uuid}>
      <div className="conv-item-head">
        <span className="conv-item-label">You</span>
        {commandName && <span className="conv-cmd-badge">{commandName}</span>}
        {eyebrow}
      </div>
      {item.text && <Markdown>{item.text}</Markdown>}
      {/* Joined prose renders above via item.text; pass only NON-text blocks to
          the walk so it doesn't double the human's prose. */}
      <MessageBlocks blocks={item.blocks.filter((b) => b.kind !== 'text')} anchorUuid={item.anchor.uuid} suppressToolUseIds={suppressToolUseIds} />
      {item.text && (
        <div className="conv-item-actions">
          <PermalinkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} />
          <CopyButton text={item.text} />
          <BookmarkButton sessionId={item.anchor.session_id} uuid={item.anchor.uuid} />
        </div>
      )}
    </div>
  );
}

export const MessageItem = memo(
  forwardRef<HTMLDivElement, {
    item: ConversationItem;
    className?: string;
    style?: React.CSSProperties;
    suppressToolUseIds?: Set<string>;
  }>(
    MessageItemImpl,
  ),
);
