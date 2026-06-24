import { Fragment, useEffect, useState } from 'react';
import { MessageItem } from './MessageItem';
import { sidechainIndentClass } from './sidechainIndent';
import { SubagentIcon } from './ConvIcons';
import { fmt } from '../lib/fmt';
import { abbreviateModel } from '../lib/modelName';
import type { ConversationItem, SubagentMeta } from '../types/conversation';
import type { SubagentNode } from './groupSidechains';

const LABEL_MAX = 60;
// #205 S3 (F7) — a roomier cap on mobile so the 2-line CSS clamp on
// .conv-sidechain-title governs the visible truncation, not this slice.
const MOBILE_LABEL_MAX = 120;

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
export function subagentSummaryLabel(items: ConversationItem[], subagentKey: string, maxLen: number = LABEL_MAX): string {
  // First NON-meta item, not items[0]: a subagent file can open with an
  // injected `meta` row (skill body / SessionStart injection) whose text would
  // otherwise leak "Base directory for this skill…" as the card title (Codex
  // P1.3). Fall back to items[0] if every item is meta.
  const root = items.find((it) => it.kind !== 'meta') ?? items[0];
  const text = root?.text ?? '';
  const firstLine = text.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? '';
  if (!firstLine) return `Subagent ${subagentKey}`;
  return firstLine.length > maxLen ? `${firstLine.slice(0, maxLen).trimEnd()}…` : firstLine;
}

// §5 — per-child render context the reader threads down so a nested subagent
// keeps the SAME machinery as a top-level one (meta lookup, force-open set, the
// suppression set, the lifted open-state + card/item refs). Passed UNCHANGED to
// every nesting level; a child `<SidechainGroup>` reads its own slice (its
// key's meta, its key's force flag) from these maps. Absent at the top level
// when the reader is rendered without children (the existing prop tests).
export interface SidechainChildContext {
  subagentMeta?: Record<string, SubagentMeta>;
  // The full ancestor-chain force-open set (§5 / Codex P1-D). A node force-opens
  // iff its key is in this set; passed down so a nested target opens too.
  forcedOpenKeys?: Set<string>;
  getItemRef?: (item: ConversationItem) => (el: HTMLDivElement | null) => void;
  getCardRef?: (rootUuid: string) => (el: HTMLElement | null) => void;
  onOpenChange?: (subagentKey: string, open: boolean) => void;
  // §5 — the spawn-chip suppression set, threaded to every member's MessageItem
  // so a grandchild's spawn chip (which lives in a CHILD thread item) is also
  // suppressed in favor of its nested card.
  suppressToolUseIds?: Set<string>;
  // #228 S2 (A3) — the loaded-spawn kind map, threaded the same way so a nested
  // thread's spawns also render the "↳ launched <kind> agent" connector.
  spawnKindByToolUseId?: Map<string, string>;
  // #205 S3 (F7) — threaded so a nested card abbreviates models + widens its
  // title cap on mobile WITHOUT a per-card matchMedia listener.
  isMobile?: boolean;
  // #232 — the render-driven jump flash uuid (Codex P0-1), threaded to every
  // nesting level so a nested card root / member flashes when it owns the jump.
  flashedUuid?: string | null;
}

// One subagent thread (one agent-*.jsonl file) as a disclosure (#155). Summary =
// task-prompt line + message count + summed thread cost. `nested` adds an indent
// class when the group hangs under a parent main item.
//
// §5 RECURSIVE NESTING — a subagent that itself spawned child subagents carries
// `children` (SubagentNode[]). They render interleaved into the body, right
// AFTER the member item whose anchor.uuid === child.spawnAnchorUuid (children
// with a null anchor append at the body end), each as a nested <SidechainGroup>
// indented one more `depth`. The `childCtx` maps thread the reader's per-key
// machinery (meta / force-open / refs / open-state) to every nesting level.
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
  meta,
  getItemRef,
  rootUuid,
  getCardRef,
  onOpenChange,
  forceOpen = false,
  riseClassName = '',
  riseStyle,
  children,
  depth = 0,
  childCtx,
  isMobile,
  flashedUuid = null,
}: {
  subagentKey: string;
  items: ConversationItem[];
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
  // #188 S4/C1 — the reader lifts this thread's open-state so the live-append
  // pill counts only VISIBLE appends (Bug 5). Fired from onToggle (user
  // collapse/expand) and from the #160 forceOpen latch (true). Keyed by
  // subagentKey — the same key the reader's openKeysRef/knownSubagentKeysRef use.
  onOpenChange?: (subagentKey: string, open: boolean) => void;
  forceOpen?: boolean;
  // G1 §4b load-in: the reader's render-time classifier passes `conv-rise`
  // (+ a per-index animationDelay) for a first-appearance top-level thread,
  // or '' to suppress (already seen, or the active jump target).
  riseClassName?: string;
  riseStyle?: React.CSSProperties;
  // §5 — child subagent threads spawned from inside THIS one. Rendered nested,
  // interleaved at each child's spawnAnchorUuid. Absent/empty for a leaf thread.
  children?: SubagentNode[];
  // §5 — this node's nesting depth (0 = top-level). Children render at depth+1.
  depth?: number;
  // §5 — the reader-threaded per-key machinery for the recursive children.
  childCtx?: SidechainChildContext;
  // #205 S3 (F7) — passed by the reader to the top-level card; nested cards
  // read childCtx.isMobile instead (threaded via renderChild).
  isMobile?: boolean;
  // #232 — the render-driven jump flash uuid (Codex P0-1). The card root flashes
  // when `flashedUuid === rootUuid`; a member flashes via its MessageItem; nested
  // cards receive it via renderChild. Unmount-safe (a class derived in render,
  // not an imperative add against a possibly-absent element).
  flashedUuid?: string | null;
}) {
  const [userOpen, setUserOpen] = useState(false);
  // #222 — latch a force into userOpen DURING RENDER (React's official "adjust
  // state when a prop changes" pattern), so the latched open-state commits
  // ATOMICALLY with forceOpen in the SAME commit — NOT a microtask later via an
  // effect. The old effect-latch raced the reader's force-RESET
  // (setForcedOpenKeys(∅), dispatched one microtask behind a synchronous
  // loadToTarget once the jump's scroll lands): when the reset won the race, a
  // nested node's parent dropped forceOpen before the parent's latch effect had
  // flushed, so the parent collapsed for a beat — UNMOUNTING the (grand)child and
  // discarding ITS pending latch. The parent then re-opened (its own latch
  // survived, never having unmounted), but the grandchild re-mounted fresh
  // (forceOpen already ∅, userOpen=false) and stayed permanently collapsed after
  // the jump (~44% of jumps under perturbed scheduling; ~0% in CI declaration
  // order, hence the latent flake). Deriving userOpen in render is
  // microtask-order-INDEPENDENT: the node never reaches open=false, so it never
  // unmounts and its latch can't be lost. Guarded by `!userOpen` so it runs at
  // most once per force (no render loop); React applies it before rendering
  // children, so the body (and any nested card) renders once, already open.
  if (forceOpen && !userOpen) setUserOpen(true);
  const open = userOpen || forceOpen;
  // #188 S4/C1 — a force-open makes this thread VISIBLE, so report it open; the
  // reader then counts a subsequent append into it (Bug 5). The OPEN-STATE itself
  // is latched in render above (the #222 fix) — this effect now carries ONLY the
  // onOpenChange side-effect (effects, not render, are where side-effects belong).
  // Keyed on forceOpen so it fires once per force, mirroring the latch.
  useEffect(() => {
    if (forceOpen) onOpenChange?.(subagentKey, true);
    // onOpenChange is a stable reader callback; keep the dep list keyed on the
    // force latch so this fires once per force.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [forceOpen]);

  const mobile = isMobile ?? childCtx?.isMobile ?? false;
  const label = subagentSummaryLabel(items, subagentKey, mobile ? MOBILE_LABEL_MAX : LABEL_MAX);
  // `in` narrows: the meta arm has neither cost_usd nor model (injected content
  // carries no turn cost / model), so guard the access instead of summing/listing
  // phantom fields.
  const cost = items.reduce((acc, it) => acc + ('cost_usd' in it ? (it.cost_usd ?? 0) : 0), 0);
  const models = [...new Set(items.map((it) => ('model' in it ? it.model : null)).filter(Boolean))] as string[];
  // #205 S3 (F7) — abbreviate + RE-dedupe on mobile (an alias id and its
  // date-stamped twin collapse to one display name).
  const modelText = (mobile ? Array.from(new Set(models.map(abbreviateModel))) : models).join(', ');

  // §5 — bucket the recursive children by the parent-thread member they anchor
  // after (child.spawnAnchorUuid). A child whose anchor is null (or whose anchor
  // is not one of THIS thread's member items) appends at the body end so it is
  // never dropped.
  const kids = children ?? [];
  const memberUuids = new Set(items.map((it) => it.anchor.uuid));
  const childrenByAnchor = new Map<string, SubagentNode[]>();
  const trailingChildren: SubagentNode[] = [];
  for (const child of kids) {
    const a = child.spawnAnchorUuid;
    if (a != null && memberUuids.has(a)) {
      const arr = childrenByAnchor.get(a);
      if (arr) arr.push(child);
      else childrenByAnchor.set(a, [child]);
    } else {
      trailingChildren.push(child);
    }
  }
  // Render one nested child <SidechainGroup>, threading the reader's per-key
  // machinery from childCtx (its own meta / force flag / refs / open-state).
  const renderChild = (child: SubagentNode) => (
    <SidechainGroup
      key={`sc-${child.subagentKey}`}
      subagentKey={child.subagentKey}
      items={child.items}
      meta={childCtx?.subagentMeta?.[child.subagentKey]}
      getItemRef={childCtx?.getItemRef}
      rootUuid={child.items[0]?.anchor.uuid}
      getCardRef={childCtx?.getCardRef}
      onOpenChange={childCtx?.onOpenChange}
      forceOpen={childCtx?.forcedOpenKeys?.has(child.subagentKey) ?? false}
      isMobile={childCtx?.isMobile}
      flashedUuid={flashedUuid}
      children={child.children}
      depth={child.depth}
      childCtx={childCtx}
    />
  );

  return (
    <details
      // #188 S3/B6 — data-uuid = the bucket-root uuid (the outline subagent
      // entry's jump anchor); ref registers the card in cardRefs UNCONDITIONALLY
      // (whether collapsed or open) so a collapsed outline click flashes THIS
      // card, not an inner member.
      data-uuid={rootUuid}
      ref={rootUuid != null && getCardRef ? getCardRef(rootUuid) : undefined}
      className={[
        'conv-sidechain',
        // #228 S2 (A2) — VISUAL hierarchy keys on `depth`, not the `nested`
        // boolean: depth-0 agents (main-spawned OR orphan) stay on the main
        // spine with the magenta dot + a stronger card and NO indent; only
        // depth >= 1 (true agent-in-agent nesting) gets the indent marker. The
        // indent magnitude is a single CSS rule reading the inline --sc-depth.
        sidechainIndentClass(depth),
        // G1 §4a: while a #160 jump force-opens this thread, snap it open
        // instantly (CSS `transition: none`) so layout is final before
        // scrollIntoView lands. The class drops when the force releases, so
        // later user toggles animate.
        forceOpen ? 'conv-sidechain--force' : '',
        // #232 — render-driven flash on a card-ROOT jump (an outline subagent
        // entry / collapsed-card flash). A member jump flashes its MessageItem.
        rootUuid != null && flashedUuid === rootUuid ? 'conv-item--jumped' : '',
        riseClassName,
      ].filter(Boolean).join(' ')}
      style={depth >= 1 ? { ...riseStyle, ['--sc-depth' as string]: String(depth) } as React.CSSProperties : riseStyle}
      open={open}
      onToggle={(e) => {
        const details = e.currentTarget as HTMLDetailsElement;
        const isOpen = details.open;
        // #228 S2 (A1) — collapsing a long thread via its (pinned) header would
        // otherwise let the native <details> collapse + the browser's
        // scroll-anchoring fling the viewport far from the card. The summary's
        // onClick added `--snap` (transition:none) so this collapse is instant;
        // now re-pin the collapsed card's header to the top of the
        // .conv-reader-body scroller so the collapse lands where the user clicked.
        // Guarded by the snap marker, so a bulk sweep (which sets .open directly,
        // with no summary click) never re-pins.
        if (!isOpen && details.classList.contains('conv-sidechain--snap')) {
          const scroller = details.closest('.conv-reader-body') as HTMLElement | null;
          if (scroller) {
            const delta = details.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
            if (delta < 0) scroller.scrollTop += delta;
          }
          details.classList.remove('conv-sidechain--snap');
        }
        setUserOpen(isOpen);
        // #188 S4/C1 — report the new open-state to the reader so the live-append
        // pill counts an append into THIS thread only while it's expanded (Bug 5).
        onOpenChange?.(subagentKey, isOpen);
      }}
    >
      <summary
        className="conv-sidechain-head"
        onClick={(e) => {
          // #228 S2 (A1) — about to collapse: suppress the height animation so the
          // collapse is instant and onToggle can re-pin the card without fighting a
          // 240ms block-size transition + scroll-anchoring. No preventDefault — the
          // native <details> toggle still runs.
          const details = e.currentTarget.parentElement as HTMLDetailsElement | null;
          if (details?.open) details.classList.add('conv-sidechain--snap');
        }}
      >
        <span className="conv-sidechain-glyph" aria-hidden="true"><SubagentIcon /></span>
        <span className="conv-sidechain-headtext">
          <span className="conv-sidechain-kind">
            Subagent{meta?.kind ? <span className="conv-sidechain-kindname"> · {meta.kind}</span> : null}
          </span>
          <span className="conv-sidechain-title">{meta?.description || label}</span>
          {meta && (meta.total_tokens != null || meta.total_duration_ms != null
                    || meta.total_tool_use_count != null || meta.status != null) && (
            <span className="conv-sidechain-submeta">
              {/* §4 1c — derived totals get a leading "~" affordance: Claude Code
                  provided none, so the figures were summed from the child's own
                  thread (approximate, not authoritative). */}
              {meta.totals_derived && <span className="conv-sidechain-derived" title="totals derived from the subagent's own thread">~</span>}
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
          {models.length > 0 && <span className="conv-sidechain-model">{modelText}</span>}
          <span>{items.length} msgs</span>
          <span className="conv-sidechain-cost">{fmt.usd2(cost)}</span>
          <span className="conv-chev" aria-hidden="true" />
        </span>
      </summary>
      <div className="conv-sidechain-body">
        {items.map((item) => {
          // §5 — interleave each child subagent thread AFTER its spawn anchor
          // item, recursively. Rendered only while THIS thread is open (its
          // members are visible). No wrapper div (a Fragment) so the body's
          // child structure / spine CSS is unchanged in the common leaf case.
          const after = open ? childrenByAnchor.get(item.anchor.uuid) : undefined;
          return (
            <Fragment key={item.anchor.uuid}>
              <MessageItem
                item={item}
                // Relies on getItemRef returning a STABLE callback per item (the
                // reader memoizes them in refCallbacks): the value is identical
                // across renders while open, so React doesn't detach/reattach and
                // MessageItem's memo isn't thrashed. Toggling open swaps it to/from
                // undefined, which is the intended detach/attach.
                ref={open && getItemRef ? getItemRef(item) : undefined}
                // §5 — suppress a spawn chip inside this thread (a child's spawn
                // lives in THIS thread's items when THIS is the parent).
                suppressToolUseIds={childCtx?.suppressToolUseIds}
                // #228 S2 (A3) — the loaded-spawn kind map so a nested-thread
                // spawn renders its connector too.
                spawnKindByToolUseId={childCtx?.spawnKindByToolUseId}
                // #232 — render-driven flash on a find-jump into THIS thread member.
                flashed={flashedUuid != null && item.member_uuids.includes(flashedUuid)}
              />
              {after?.map(renderChild)}
            </Fragment>
          );
        })}
        {/* §5 — children with no resolvable spawn anchor append at the body end
            (never dropped). Also only while open. */}
        {open && trailingChildren.map(renderChild)}
      </div>
    </details>
  );
}
