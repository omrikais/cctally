import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SidechainGroup, subagentSummaryLabel } from './SidechainGroup';
import { SUBAGENT_WINDOW_CAP, SUBAGENT_WINDOW_CHUNK } from './subagentWindow';
import type { SubagentNode } from './groupSidechains';
import type { ConversationItem } from '../types/conversation';

function member(uuid: string, over: Partial<ConversationItem> = {}): ConversationItem {
  return {
    kind: 'human',
    anchor: { session_id: 's', uuid, id: 0 },
    member_uuids: [uuid],
    ts: 't',
    text: uuid,
    blocks: [],
    is_sidechain: true,
    subagent_key: 'k1',
    parent_uuid: null,
    ...over,
  } as ConversationItem;
}

describe('subagentSummaryLabel', () => {
  it('uses the first non-blank line of the root prose, truncated', () => {
    const long = 'Port the fixture harness to the new builder and regenerate every golden file now';
    const items = [member('r', { text: `\n  ${long}\nsecond line` })];
    const label = subagentSummaryLabel(items, 'hash');
    expect(label.startsWith('Port the fixture harness')).toBe(true);
    expect(label.endsWith('…')).toBe(true);
    expect(label.length).toBeLessThanOrEqual(61); // 60 + ellipsis
  });

  it('falls back to "Subagent <hash>" when the root has no prose', () => {
    expect(subagentSummaryLabel([member('r', { text: '   ' })], 'abcd1234')).toBe('Subagent abcd1234');
  });

  it('skips a leading meta item so an injected skill body never becomes the title (Codex P1.3)', () => {
    const items = [
      member('m', {
        kind: 'meta',
        text: 'Base directory for this skill: /x/skills/brainstorming\n\nbody',
        meta_kind: 'skill',
        skill_name: 'brainstorming',
      } as Partial<ConversationItem>),
      member('r', { text: 'Audit the cache layer' }),
    ];
    expect(subagentSummaryLabel(items, 'hash')).toBe('Audit the cache layer');
  });

  it('falls back to items[0] when EVERY item is meta', () => {
    const items = [
      member('m', { kind: 'meta', text: '## Git Context', meta_kind: 'context', skill_name: null } as Partial<ConversationItem>),
    ];
    expect(subagentSummaryLabel(items, 'h')).toBe('## Git Context');
  });
});

describe('SidechainGroup', () => {
  it('renders a collapsed disclosure with label, count, and summed cost', () => {
    const items = [
      member('s1', { kind: 'assistant', text: 'Audit module A', cost_usd: 0.30 } as Partial<ConversationItem>),
      member('s2', { kind: 'assistant', text: '', cost_usd: 0.12 } as Partial<ConversationItem>),
    ];
    const { container } = render(<SidechainGroup subagentKey="k1" items={items} />);
    const details = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(details).not.toBeNull();
    expect(details.open).toBe(false);
    expect(details.classList.contains('conv-sidechain--nested')).toBe(false);
    const summary = details.querySelector('summary')!.textContent!;
    expect(summary).toContain('Audit module A'); // label from first member prose
    expect(summary).toContain('2 msgs');
    expect(summary).toContain('$0.42');           // 0.30 + 0.12
  });

  it('keeps a depth-0 card OFF the indent ladder (no --nested), even when placed under a main turn', () => {
    // depth stays 0 even for a main-spawned agent (where:'main'); only true
    // agent-in-agent nesting (depth >= 1) indents.
    const { container } = render(<SidechainGroup subagentKey="k1" items={[member('s1')]} depth={0} />);
    expect(container.querySelector('details.conv-sidechain')!.classList.contains('conv-sidechain--nested')).toBe(false);
  });

  it('applies the --nested indent class and sets --sc-depth at depth >= 1 (true nesting)', () => {
    const { container } = render(<SidechainGroup subagentKey="k1" items={[member('s1')]} depth={2} />);
    const details = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(details.classList.contains('conv-sidechain--nested')).toBe(true);
    expect(details.style.getPropertyValue('--sc-depth')).toBe('2');
  });

  it('renders each member as a MessageItem in the body', () => {
    const { container } = render(<SidechainGroup subagentKey="k1" items={[member('s1'), member('s2')]} />);
    expect(container.querySelectorAll('.conv-sidechain-body .conv-item')).toHaveLength(2);
    expect(container.querySelector('[data-uuid="s1"]')).not.toBeNull();
  });

  it('renders the card header: glyph, static Subagent eyebrow, serif title, meta', () => {
    const items = [
      member('r', { kind: 'assistant', text: 'Audit module A', model: 'claude-opus-4', cost_usd: 0.30 } as Partial<ConversationItem>),
      member('s2', { kind: 'assistant', text: '', model: 'claude-opus-4', cost_usd: 0.12 } as Partial<ConversationItem>),
    ];
    render(<SidechainGroup subagentKey="aaaa1111" items={items} />);
    // The eyebrow leads with the static "Subagent" word (Q1). With no meta it
    // is the whole eyebrow text (no kindname child). Class-based, not getByText,
    // since the kind span becomes a child when meta is present.
    expect(document.querySelector('.conv-sidechain-kind')!.textContent).toBe('Subagent');
    expect(document.querySelector('.conv-sidechain-title')).toBeTruthy();
    expect(document.querySelector('.conv-sidechain-head .conv-chev')).toBeTruthy();
    expect(screen.getByText(`${items.length} msgs`)).toBeInTheDocument();
    // C3: the glyph is now an inline SVG (not the 🧵 emoji).
    const glyph = document.querySelector('.conv-sidechain-glyph')!;
    expect(glyph.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(glyph.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
  });

  it('renders the kind in the eyebrow when meta.kind is present', () => {
    const items = [member('r', { kind: 'assistant', text: 'Audit module A', cost_usd: 0.30 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="aaaa1111" items={items}
             meta={{ kind: 'Explore', total_tokens: 23285, total_duration_ms: 10668,
                     total_tool_use_count: 1, status: 'completed' }} />);
    expect(document.querySelector('.conv-sidechain-kindname')!.textContent).toContain('Explore');
  });

  it('renders the toolUseResult meta line', () => {
    const items = [member('r', { kind: 'assistant', text: 'Audit module A', cost_usd: 0.30 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="aaaa1111" items={items}
             meta={{ kind: 'Explore', total_tokens: 23285, total_duration_ms: 10668,
                     total_tool_use_count: 1, status: 'completed' }} />);
    const sub = document.querySelector('.conv-sidechain-submeta')!;
    expect(sub.textContent).toContain('23.3k tok');
    expect(sub.textContent).toContain('10.7s');
    expect(sub.textContent).toContain('1 tool');
    expect(sub.querySelector('.conv-subagent-ok')).toBeTruthy();  // ✓ for completed
  });

  it('spells out a failure status with the error class', () => {
    const items = [member('r', { kind: 'assistant', text: 'Audit module B', cost_usd: 0.30 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="bbbb2222" items={items}
             meta={{ kind: 'code-reviewer', status: 'error' }} />);
    const err = document.querySelector('.conv-subagent-err')!;
    expect(err.textContent).toContain('error');
  });

  it('spells out a non-completed terminal status (⚠ <status>) with the warn class', () => {
    const items = [member('r', { kind: 'assistant', text: 'Audit module D', cost_usd: 0.30 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="dddd4444" items={items}
             meta={{ kind: 'Explore', status: 'aborted' }} />);
    const warn = document.querySelector('.conv-subagent-warn')!;
    expect(warn.textContent).toContain('aborted');
    expect(warn.textContent).toContain('⚠');
  });

  it('renders the kind eyebrow but no submeta line when only kind is present (no-blank-line guard)', () => {
    const items = [member('r', { kind: 'assistant', text: 'Audit module E', cost_usd: 0.30 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="eeee5555" items={items}
             meta={{ kind: 'Explore' }} />);
    expect(document.querySelector('.conv-sidechain-kindname')!.textContent).toContain('Explore');
    expect(document.querySelector('.conv-sidechain-submeta')).toBeNull();
  });

  it('falls back to title-only when meta is absent (no kind, no submeta line)', () => {
    const items = [member('r', { kind: 'assistant', text: 'Audit module C', cost_usd: 0.30 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="cccc3333" items={items} />);
    expect(document.querySelector('.conv-sidechain-kindname')).toBeNull();
    expect(document.querySelector('.conv-sidechain-submeta')).toBeNull();
    expect(document.querySelector('.conv-sidechain-kind')!.textContent).toBe('Subagent');
  });

  it('pluralizes the tool count (0 tools / 2 tools)', () => {
    const items = [member('r', { kind: 'assistant', text: 'x', cost_usd: 0 } as Partial<ConversationItem>)];
    const { rerender } = render(<SidechainGroup subagentKey="k" items={items}
             meta={{ kind: 'Explore', total_tool_use_count: 0 }} />);
    expect(document.querySelector('.conv-sidechain-submeta')!.textContent).toContain('0 tools');
    rerender(<SidechainGroup subagentKey="k" items={items}
             meta={{ kind: 'Explore', total_tool_use_count: 2 }} />);
    expect(document.querySelector('.conv-sidechain-submeta')!.textContent).toContain('2 tools');
  });

  it('applies conv-sidechain--force only while forceOpen (G1 §4a #160 instant-open)', () => {
    const items = [member('s1'), member('s2')];
    const { container, rerender } = render(
      <SidechainGroup subagentKey="k1" items={items} forceOpen={false} />,
    );
    const details = container.querySelector('details.conv-sidechain')!;
    expect(details).not.toHaveClass('conv-sidechain--force');
    rerender(<SidechainGroup subagentKey="k1" items={items} forceOpen={true} />);
    expect(details).toHaveClass('conv-sidechain--force');
  });

  // #188 S3/B6 — the collapsed-card DOM anchor. The <details> carries
  // data-uuid={rootUuid} and registers itself in a separate cardRefs map via
  // getCardRef, UNCONDITIONALLY (open and closed) — so an outline subagent click
  // (jump to the bucket-root uuid) resolves the card while collapsed and flashes
  // it, instead of force-opening + flashing an inner member (Bug 1).
  it('puts data-uuid={rootUuid} on the <details> and registers it via getCardRef while collapsed', () => {
    const cardRefs = new Map<string, HTMLElement>();
    const getCardRef = (rootUuid: string) => (el: HTMLElement | null) => {
      if (el) cardRefs.set(rootUuid, el);
      else cardRefs.delete(rootUuid);
    };
    const items = [member('root', { kind: 'assistant', text: 'task' } as Partial<ConversationItem>), member('s2')];
    const { container } = render(
      <SidechainGroup subagentKey="k1" items={items} rootUuid="root" getCardRef={getCardRef} />,
    );
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(det.open).toBe(false);                 // collapsed
    expect(det.getAttribute('data-uuid')).toBe('root');
    // The card element registered while collapsed (the inner members did NOT —
    // they stay ref-less until open).
    expect(cardRefs.get('root')).toBe(det);
  });

  it('keeps the card registered after the thread is forced open (unconditional)', () => {
    const cardRefs = new Map<string, HTMLElement>();
    const getCardRef = (rootUuid: string) => (el: HTMLElement | null) => {
      if (el) cardRefs.set(rootUuid, el);
      else cardRefs.delete(rootUuid);
    };
    const items = [member('root'), member('s2')];
    const base = { subagentKey: 'k1', items, rootUuid: 'root', getCardRef };
    const { container, rerender } = render(<SidechainGroup {...base} forceOpen={false} />);
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(cardRefs.get('root')).toBe(det);
    rerender(<SidechainGroup {...base} forceOpen={true} />);
    expect(det.open).toBe(true);
    // Still registered when open — no key collision, no open/close toggle race.
    expect(cardRefs.get('root')).toBe(det);
  });

  // #188 S4/C1 — the reader lifts subagent open-state so it can count only
  // VISIBLE live appends (Bug 5). onOpenChange fires with the subagent key + the
  // new open state on a user toggle, and `true` when a #160 force-open latches.
  it('fires onOpenChange(subagentKey, true/false) on a user toggle', () => {
    const onOpenChange = vi.fn();
    const items = [member('s1'), member('s2')];
    const { container } = render(
      <SidechainGroup subagentKey="k1" items={items} onOpenChange={onOpenChange} />,
    );
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    // jsdom doesn't fire `toggle` from a property set; simulate the user opening
    // the disclosure by setting `open` then dispatching the native `toggle`
    // event the React onToggle handler binds to (a real browser <details> fires
    // this on a summary click).
    det.open = true;
    fireEvent(det, new Event('toggle', { bubbles: false }));
    expect(onOpenChange).toHaveBeenCalledWith('k1', true);
    det.open = false;
    fireEvent(det, new Event('toggle', { bubbles: false }));
    expect(onOpenChange).toHaveBeenCalledWith('k1', false);
  });

  // #232 (Codex P1-4) — the depth-0 click-collapse re-pin goes THROUGH Virtuoso
  // (`pinToSelf` → the reader's scrollToIndex), NOT a raw `scrollTop +=` write on
  // the scroller. The summary's onClick arms the `--snap` marker on the open card;
  // the collapse `toggle` then calls `pinToSelf` (guarded by that marker) and
  // clears the marker. A bulk sweep (sets `.open` with no summary click → no
  // `--snap`) must NOT re-pin.
  it('calls pinToSelf on a user click-collapse (through Virtuoso, not raw scrollTop) (#232)', () => {
    const pinToSelf = vi.fn();
    const items = [member('s1'), member('s2')];
    const { container } = render(
      <SidechainGroup subagentKey="k1" items={items} pinToSelf={pinToSelf} />,
    );
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    // Open the card first.
    det.open = true;
    fireEvent(det, new Event('toggle', { bubbles: false }));
    // The summary onClick arms `--snap` (suppress the height animation) just before
    // the native collapse — mirror a real click on the (now-open) header.
    const summary = det.querySelector('summary')!;
    fireEvent.click(summary);
    expect(det.classList.contains('conv-sidechain--snap')).toBe(true);
    // Now the native collapse fires `toggle` with open=false → re-pin via Virtuoso.
    det.open = false;
    fireEvent(det, new Event('toggle', { bubbles: false }));
    expect(pinToSelf).toHaveBeenCalledTimes(1);
    // The snap marker is cleared so later toggles animate again.
    expect(det.classList.contains('conv-sidechain--snap')).toBe(false);
  });

  it('does NOT call pinToSelf on a collapse with no --snap (e.g. a bulk sweep) (#232)', () => {
    const pinToSelf = vi.fn();
    const items = [member('s1'), member('s2')];
    const { container } = render(
      <SidechainGroup subagentKey="k1" items={items} pinToSelf={pinToSelf} />,
    );
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    // Open then collapse WITHOUT a summary click (no `--snap`), as a bulk sweep does
    // (it sets `.open` directly). The re-pin must not fire.
    det.open = true;
    fireEvent(det, new Event('toggle', { bubbles: false }));
    det.open = false;
    fireEvent(det, new Event('toggle', { bubbles: false }));
    expect(pinToSelf).not.toHaveBeenCalled();
  });

  it('fires onOpenChange(subagentKey, true) when a force-open latches', () => {
    const onOpenChange = vi.fn();
    const items = [member('s1'), member('s2')];
    const base = { subagentKey: 'k1', items, onOpenChange };
    const { rerender } = render(<SidechainGroup {...base} forceOpen={false} />);
    expect(onOpenChange).not.toHaveBeenCalled();
    // Force the thread open: the latch effect must report the key as open so the
    // reader counts a subsequent append into THIS now-visible thread.
    rerender(<SidechainGroup {...base} forceOpen={true} />);
    expect(onOpenChange).toHaveBeenCalledWith('k1', true);
  });

  // #232 — a sidechain scrolled OFF-SCREEN under virtualization UNMOUNTS. If the
  // user then hits expand-all/collapse-all (`]`/`[`, advancing bulkSweep.rev) and
  // scrolls the group back into view, it REMOUNTS while bulkSweep.rev is already
  // advanced. A fresh mount must ADOPT the latest sweep (rev > 0) — the whole
  // reason the sweep moved to the data model (Codex P1-1) is to reach groups that
  // were unmounted during the sweep. The render-all mock never unmounts, so this
  // needs a DIRECT mount with rev already advanced.
  it('adopts an already-advanced bulk sweep on a fresh (re)mount (#232 off-screen group)', () => {
    const items = [member('s1'), member('s2')];
    // Fresh mount with rev already at 2 (the group was off-screen when the user
    // hit expand-all): it must render OPEN, adopting the swept open-state.
    const { container } = render(
      <SidechainGroup subagentKey="k1" items={items} bulkSweep={{ rev: 2, open: true }} />,
    );
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(det.open).toBe(true);
  });

  it('renders collapsed by default on a no-sweep mount (rev 0 / undefined — never spuriously open)', () => {
    const items = [member('s1'), member('s2')];
    // rev 0 = "no sweep yet" → the group keeps its natural collapsed default even
    // though the sweep open-state is true (it was never actually swept).
    const { container } = render(
      <SidechainGroup subagentKey="k1" items={items} bulkSweep={{ rev: 0, open: true }} />,
    );
    expect((container.querySelector('details.conv-sidechain') as HTMLDetailsElement).open).toBe(false);
    // And with no bulkSweep prop at all.
    const { container: c2 } = render(<SidechainGroup subagentKey="k2" items={items} />);
    expect((c2.querySelector('details.conv-sidechain') as HTMLDetailsElement).open).toBe(false);
  });

  it('opens on forceOpen, registers member refs only while open, and latches open after the force drops', () => {
    const refs = new Map<string, HTMLDivElement>();
    const getItemRef = (item: ConversationItem) => (el: HTMLDivElement | null) => {
      for (const u of item.member_uuids) {
        if (el) refs.set(u, el);
        else refs.delete(u);
      }
    };
    const items = [member('s1'), member('s2')];
    const base = { subagentKey: 'k1', items, getItemRef };

    const { container, rerender } = render(<SidechainGroup {...base} forceOpen={false} />);
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(det.open).toBe(false);
    expect(refs.size).toBe(0); // collapsed → members are ref-less

    // Force the thread open: it opens AND its members attach refs in that commit.
    rerender(<SidechainGroup {...base} forceOpen={true} />);
    expect(det.open).toBe(true);
    expect(refs.get('s1')).toBeTruthy();
    expect(refs.get('s2')).toBeTruthy();

    // Drop the force: the latch keeps it open (user jumped there to read it).
    rerender(<SidechainGroup {...base} forceOpen={false} />);
    expect(det.open).toBe(true);
    expect(refs.get('s1')).toBeTruthy();
  });
});

describe('SidechainGroup — recursive nesting (§5)', () => {
  // A child node carrying ONE grandchild subagent in `children`, spawned after
  // the child's `c1` member item (spawnAnchorUuid: 'c1'), depth 1.
  const grandchild: SubagentNode = {
    kind: 'subagent',
    subagentKey: 'ace20002',
    items: [member('g1', { kind: 'assistant', text: 'Ground claims', cost_usd: 0.2 } as Partial<ConversationItem>)],
    nested: true,
    depth: 1,
    spawnAnchorUuid: 'c1',
    children: [],
  };

  it('renders a nested grandchild card inside the (force-open) child body, indented by depth', () => {
    const childItems = [member('c1', { kind: 'assistant', text: 'Sync audit', cost_usd: 0.3 } as Partial<ConversationItem>)];
    const { container } = render(
      <SidechainGroup
        subagentKey="cc110001"
        items={childItems}
        meta={{ kind: 'code-reviewer', description: 'Sync audit' }}
        forceOpen={true}                       // open so the body (and child) render
        children={[grandchild]}
        depth={0}
        childCtx={{
          subagentMeta: { ace20002: { kind: 'grounding', description: 'Ground claims', totals_derived: true,
                                      total_tokens: 8400, total_tool_use_count: 5 } },
          forcedOpenKeys: new Set(['ace20002']),  // force the grandchild open too
        }}
      />,
    );
    // Two sidechain cards: the parent + the nested grandchild.
    const cards = container.querySelectorAll('details.conv-sidechain');
    expect(cards).toHaveLength(2);
    // The grandchild card carries the deeper nesting class and its own data-uuid.
    const gc = container.querySelector('[data-uuid="g1"]') as HTMLElement;
    expect(gc).not.toBeNull();
    expect(gc.tagName.toLowerCase()).toBe('details');
    expect(gc.classList.contains('conv-sidechain--nested')).toBe(true);
    // The grandchild's kind + description surface from its own meta.
    expect(gc.querySelector('.conv-sidechain-kindname')!.textContent).toContain('grounding');
    expect(gc.querySelector('.conv-sidechain-title')!.textContent).toBe('Ground claims');
  });

  it('does NOT render nested children while the parent thread is collapsed', () => {
    const childItems = [member('c1', { kind: 'assistant', text: 'Sync audit', cost_usd: 0.3 } as Partial<ConversationItem>)];
    const { container } = render(
      <SidechainGroup subagentKey="cc110001" items={childItems}
        forceOpen={false} children={[grandchild]} depth={0} childCtx={{}} />,
    );
    // Collapsed parent → exactly one card (no grandchild rendered yet).
    expect(container.querySelectorAll('details.conv-sidechain')).toHaveLength(1);
    expect(container.querySelector('[data-uuid="g1"]')).toBeNull();
  });

  it('renders the "~" derived-totals affordance when meta.totals_derived is true', () => {
    const items = [member('r', { kind: 'assistant', text: 'Background audit', cost_usd: 0.1 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="a55c0003" items={items}
      meta={{ kind: 'Explore', total_tokens: 2300, total_duration_ms: 4000,
              total_tool_use_count: 1, status: 'completed', totals_derived: true }} />);
    const sub = document.querySelector('.conv-sidechain-submeta')!;
    // The leading "~" marks the figures as derived (not authoritative).
    expect(sub.querySelector('.conv-sidechain-derived')!.textContent).toBe('~');
    expect(sub.textContent).toContain('2.3k tok');
    expect(sub.querySelector('.conv-subagent-ok')).toBeTruthy(); // ✓ for async completion
  });

  it('omits the "~" affordance when totals are authoritative (no totals_derived)', () => {
    const items = [member('r', { kind: 'assistant', text: 'Sync audit', cost_usd: 0.1 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="cc110001" items={items}
      meta={{ kind: 'code-reviewer', total_tokens: 12000, status: 'completed' }} />);
    expect(document.querySelector('.conv-sidechain-submeta .conv-sidechain-derived')).toBeNull();
  });

  it('appends a child with no resolvable spawn anchor at the body end (never dropped)', () => {
    const orphanChild: SubagentNode = {
      kind: 'subagent', subagentKey: 'orphan9', items: [member('og1', { kind: 'assistant', text: 'Orphan child' } as Partial<ConversationItem>)],
      nested: true, depth: 1, spawnAnchorUuid: 'NOT_A_MEMBER', children: [],
    };
    const childItems = [member('c1', { kind: 'assistant', text: 'Parent', cost_usd: 0 } as Partial<ConversationItem>)];
    const { container } = render(
      <SidechainGroup subagentKey="par" items={childItems}
        forceOpen={true} children={[orphanChild]} depth={0}
        childCtx={{ forcedOpenKeys: new Set() }} />,
    );
    // The orphan child still renders (appended at the end), never dropped.
    expect(container.querySelector('[data-uuid="og1"]')).not.toBeNull();
  });
});

describe('SidechainGroup header (#193)', () => {
  it('shows meta.description as the title when present', () => {
    const items = [member('u1', { kind: 'human', text: 'long raw prompt blob...' })];
    render(<SidechainGroup subagentKey="abc" items={items}
      meta={{ kind: 'general-purpose', description: 'Code review Phase A' }} />);
    expect(screen.getByText('Code review Phase A')).toBeTruthy();
    // The first-prompt label must NOT win when a description is present.
    expect(document.querySelector('.conv-sidechain-title')!.textContent).toBe('Code review Phase A');
  });

  it('falls back to the first-prompt label when no description', () => {
    const items = [member('u1', { kind: 'human', text: 'Do the analysis' })];
    render(<SidechainGroup subagentKey="abc" items={items}
      meta={{ kind: 'general-purpose' }} />);
    // "Do the analysis" also appears in the rendered member body, so scope to
    // the title span — the label fallback must still drive the header title.
    expect(document.querySelector('.conv-sidechain-title')!.textContent).toBe('Do the analysis');
  });
});

describe('SidechainGroup title tooltip (#238 R4)', () => {
  // A first-line prompt longer than LABEL_MAX (60) so the VISIBLE label truncates
  // with an ellipsis, but the title= attribute carries the FULL untruncated text.
  const long = 'Map the whole conversation viewer surface and locate every find-landing branch precisely';

  it('carries the FULL untruncated first-line label in title= when no meta.description', () => {
    const { container } = render(
      <SidechainGroup subagentKey="k1" items={[member('r', { text: long })]} />,
    );
    const titleEl = container.querySelector('.conv-sidechain-title')!;
    // Visible text is truncated; the tooltip is the complete text.
    expect(titleEl.textContent!.endsWith('…')).toBe(true);
    expect(titleEl.getAttribute('title')).toBe(long);
    expect(titleEl.getAttribute('title')).not.toContain('…');
  });

  it('uses meta.description (full) as the title when present', () => {
    const desc = 'Code review Phase A — full untruncated description text used as the tooltip';
    const { container } = render(
      <SidechainGroup subagentKey="k1" items={[member('r', { text: long })]}
        meta={{ kind: 'general-purpose', description: desc }} />,
    );
    expect(container.querySelector('.conv-sidechain-title')!.getAttribute('title')).toBe(desc);
  });
});

describe('SidechainGroup windowing (#239)', () => {
  // N human members for one subagent thread; uuid u<i>, text "member <i>".
  // Member PRESENCE is asserted via data-uuid (MessageItem stamps data-uuid on
  // every variant root), NOT prose text — the card title is derived from
  // items[0].text, so a text query for the head member would also match the
  // (always-rendered) title span.
  function bigThread(n: number): ConversationItem[] {
    return Array.from({ length: n }, (_, i) => member(`u${i}`, { text: `member ${i}` }));
  }
  const has = (c: HTMLElement, uuid: string) => c.querySelector(`.conv-sidechain-body [data-uuid="${uuid}"]`) != null;

  it('renders all members at/under cap (no reveal controls)', () => {
    const its = bigThread(SUBAGENT_WINDOW_CAP);
    const { container } = render(<SidechainGroup subagentKey="k" items={its} forceOpen />);
    expect(has(container, 'u0')).toBe(true);
    expect(has(container, `u${SUBAGENT_WINDOW_CAP - 1}`)).toBe(true);
    expect(screen.queryByRole('button', { name: /earlier|later|Show all/ })).toBeNull();
  });

  it('windows an over-cap thread to a head slice + "later"/"all" controls', () => {
    const n = SUBAGENT_WINDOW_CAP + 200;
    const { container } = render(<SidechainGroup subagentKey="k" items={bigThread(n)} forceOpen />);
    // head-anchored: first CAP members rendered, the tail not.
    expect(has(container, 'u0')).toBe(true);
    expect(has(container, `u${n - 1}`)).toBe(false);
    expect(screen.getByRole('button', { name: new RegExp(`Show ${SUBAGENT_WINDOW_CHUNK} later`) })).toBeTruthy();
    expect(screen.getByRole('button', { name: new RegExp(`Show all ${n}`) })).toBeTruthy();
    // no "earlier" at the head.
    expect(screen.queryByRole('button', { name: /Show .* earlier/ })).toBeNull();
  });

  it('centers the window on a deep windowAnchorUuid (own member)', () => {
    const n = SUBAGENT_WINDOW_CAP + 400;
    const targetIdx = SUBAGENT_WINDOW_CAP + 250;
    const { container } = render(<SidechainGroup subagentKey="k" items={bigThread(n)} forceOpen windowAnchorUuid={`u${targetIdx}`} />);
    expect(has(container, `u${targetIdx}`)).toBe(true); // target mounted
    expect(has(container, 'u0')).toBe(false);           // head trimmed
    expect(screen.getByRole('button', { name: /earlier/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /later/ })).toBeTruthy();
  });

  it('"Show all" reveals every member', () => {
    const n = SUBAGENT_WINDOW_CAP + 200;
    const { container } = render(<SidechainGroup subagentKey="k" items={bigThread(n)} forceOpen />);
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`Show all ${n}`) }));
    expect(has(container, `u${n - 1}`)).toBe(true);
  });

  it('"Show later" grows the window by CHUNK', () => {
    const n = SUBAGENT_WINDOW_CAP + 200;
    const { container } = render(<SidechainGroup subagentKey="k" items={bigThread(n)} forceOpen />);
    expect(has(container, `u${SUBAGENT_WINDOW_CAP}`)).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`Show ${SUBAGENT_WINDOW_CHUNK} later`) }));
    expect(has(container, `u${SUBAGENT_WINDOW_CAP}`)).toBe(true); // first hidden-after now shown
  });

  it('reveal buttons carry data-conv-marker (j/k skips them)', () => {
    render(<SidechainGroup subagentKey="k" items={bigThread(SUBAGENT_WINDOW_CAP + 10)} forceOpen />);
    const btn = screen.getByRole('button', { name: /later/ });
    expect(btn.getAttribute('data-conv-marker')).toBe('');
  });

  it('mounts a nested DIRECT-CHILD card when its anchor sits past the parent head window', () => {
    // Parent of n members; a child spawns after a member OUTSIDE the head window;
    // the anchor lives in the direct child. The child card must still mount (P0)
    // because the parent centers its window on the child's spawn-anchor member.
    const n = SUBAGENT_WINDOW_CAP + 300;
    const parent = bigThread(n);
    const childSpawn = parent[SUBAGENT_WINDOW_CAP + 100].anchor.uuid;
    const childItems = Array.from({ length: 20 }, (_, i) => member(`c${i}`, { text: `child ${i}`, subagent_key: 'k2' } as Partial<ConversationItem>));
    const child: SubagentNode = {
      kind: 'subagent', subagentKey: 'k2', items: childItems, nested: true, depth: 1, spawnAnchorUuid: childSpawn, children: [],
    };
    const { container } = render(
      <SidechainGroup
        subagentKey="k" items={parent} forceOpen
        windowAnchorUuid="c10"
        // force the child open too (the reader force-opens the whole ancestor chain)
        childCtx={{ forcedOpenKeys: new Set(['k2']) }}
        children={[child]}
      />,
    );
    // The child card mounted because the parent centered on the child's spawn member.
    expect(container.querySelector('[data-uuid="c10"]')).not.toBeNull();
    // Sanity: head member trimmed (the parent IS windowed past the head).
    expect(has(container, 'u0')).toBe(false);
  });

  it('keeps a retained member as the SAME DOM node when the window grows (memo non-regression #231)', () => {
    // Slicing changes mount/unmount only — a retained MessageItem's props must NOT
    // churn. Growing the window via "Show later" keeps the head member's element
    // referentially identical (React reused it: no remount, no key/prop churn that
    // would have forced a replace).
    const n = SUBAGENT_WINDOW_CAP + 200;
    const { container } = render(<SidechainGroup subagentKey="k" items={bigThread(n)} forceOpen />);
    const m0a = container.querySelector('[data-uuid="u0"]');
    expect(m0a).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`Show ${SUBAGENT_WINDOW_CHUNK} later`) }));
    const m0b = container.querySelector('[data-uuid="u0"]');
    expect(m0b).toBe(m0a);
  });

  it('mounts a nested GRANDCHILD card when its anchor sits deep in the tree (P0 recursive)', () => {
    // parent k (over-cap) -> child k2 (spawns PAST the parent head window) ->
    // grandchild k3 (member g10 is the anchor). The parent must center on k2's
    // spawn member; k2 (sub-cap, renders all) mounts k3; k3 centers on g10. This
    // exercises the full recursive resolveSubagentAnchorIndex path, not just a
    // direct child (the pure helper covers the math; this proves the wiring).
    const n = SUBAGENT_WINDOW_CAP + 300;
    const parent = bigThread(n);
    const childSpawn = parent[SUBAGENT_WINDOW_CAP + 100].anchor.uuid;
    const childItems = Array.from({ length: 20 }, (_, i) => member(`c${i}`, { text: `child ${i}`, subagent_key: 'k2' } as Partial<ConversationItem>));
    const grandItems = Array.from({ length: 30 }, (_, i) => member(`g${i}`, { text: `grand ${i}`, subagent_key: 'k3' } as Partial<ConversationItem>));
    const grand: SubagentNode = { kind: 'subagent', subagentKey: 'k3', items: grandItems, nested: true, depth: 2, spawnAnchorUuid: 'c5', children: [] };
    const child: SubagentNode = { kind: 'subagent', subagentKey: 'k2', items: childItems, nested: true, depth: 1, spawnAnchorUuid: childSpawn, children: [grand] };
    const { container } = render(
      <SidechainGroup
        subagentKey="k" items={parent} forceOpen
        windowAnchorUuid="g10"
        childCtx={{ forcedOpenKeys: new Set(['k2', 'k3']) }}
        children={[child]}
      />,
    );
    // The grandchild's own member is mounted: the whole ancestor path centered.
    expect(container.querySelector('[data-uuid="g10"]')).not.toBeNull();
    expect(has(container, 'u0')).toBe(false); // parent head trimmed
  });

  it('re-centers when the anchor CHANGES to a windowed-out member (adjust-state-on-prop-change)', () => {
    // The change's riskiest line: the re-center fires IN RENDER when
    // windowAnchorUuid changes. Mount head-anchored (deep member trimmed), then
    // rerender with the anchor on a windowed-out member and assert it re-centers.
    const n = SUBAGENT_WINDOW_CAP + 400;
    const targetIdx = SUBAGENT_WINDOW_CAP + 250;
    const its = bigThread(n);
    const { container, rerender } = render(<SidechainGroup subagentKey="k" items={its} forceOpen />);
    expect(has(container, `u${targetIdx}`)).toBe(false); // windowed out at the head
    expect(has(container, 'u0')).toBe(true);
    rerender(<SidechainGroup subagentKey="k" items={its} forceOpen windowAnchorUuid={`u${targetIdx}`} />);
    expect(has(container, `u${targetIdx}`)).toBe(true);  // re-centered: target now mounted
    expect(has(container, 'u0')).toBe(false);            // head dropped
  });

  it('keeps a retained member the SAME DOM node when "Show earlier" inserts above it (memo #231)', () => {
    // The true analog of the #231 prepend cascade: content inserted ABOVE shifts
    // every retained member's position, but keys are item.anchor.uuid and props
    // are item-derived, so React reuses the elements (no remount).
    const n = SUBAGENT_WINDOW_CAP + 400;
    const targetIdx = SUBAGENT_WINDOW_CAP + 250;
    const { container } = render(<SidechainGroup subagentKey="k" items={bigThread(n)} forceOpen windowAnchorUuid={`u${targetIdx}`} />);
    const ta = container.querySelector(`[data-uuid="u${targetIdx}"]`);
    expect(ta).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /earlier/ }));
    const tb = container.querySelector(`[data-uuid="u${targetIdx}"]`);
    expect(tb).toBe(ta); // same node across an insert-above reveal
  });
});

describe('subagentSummaryLabel maxLen (#205 S3 F7)', () => {
  const prompt = 'x'.repeat(100); // 100 chars, no newline

  it('defaults to a 60-char cap (existing behavior)', () => {
    const label = subagentSummaryLabel([member('r', { text: prompt })], 'h');
    expect(label.length).toBe(61); // 60 + ellipsis
    expect(label.endsWith('…')).toBe(true);
  });

  it('honors a larger explicit maxLen', () => {
    const label = subagentSummaryLabel([member('r', { text: prompt })], 'h', 120);
    expect(label).toBe(prompt); // 100 < 120 → no truncation
  });
});

describe('SidechainGroup mobile model abbreviation (#205 S3 F7)', () => {
  // Two distinct ids that collapse to the SAME abbreviation — must de-dupe.
  const items = [
    member('s1', { kind: 'assistant', text: 'Audit', cost_usd: 0.1, model: 'claude-opus-4-8-20251101' } as Partial<ConversationItem>),
    member('s2', { kind: 'assistant', text: 'More',  cost_usd: 0.1, model: 'claude-opus-4-8' } as Partial<ConversationItem>),
  ];

  it('abbreviates + de-duplicates the model list when isMobile', () => {
    const { container } = render(<SidechainGroup subagentKey="k1" items={items} isMobile />);
    const model = container.querySelector('.conv-sidechain-model')!.textContent!;
    expect(model).toBe('opus-4-8');                 // both collapse to one
    expect(model).not.toContain('claude-');
    expect(model).not.toContain('20251101');
  });

  it('renders the full ids on desktop (isMobile false)', () => {
    const { container } = render(<SidechainGroup subagentKey="k1" items={items} isMobile={false} />);
    const model = container.querySelector('.conv-sidechain-model')!.textContent!;
    expect(model).toContain('claude-opus-4-8-20251101');
    expect(model).toContain('claude-opus-4-8');
  });

  it('shows >60 chars of a no-meta fallback title on mobile (MOBILE_LABEL_MAX path)', () => {
    const long = 'Implement the conversation reader find bar and wire up the n and N step bindings cleanly';
    const longItems = [member('r', { kind: 'assistant', text: long, cost_usd: 0 } as Partial<ConversationItem>)];
    const { container } = render(<SidechainGroup subagentKey="k1" items={longItems} isMobile />);
    const title = container.querySelector('.conv-sidechain-title')!.textContent!;
    expect(title.length).toBeGreaterThan(61); // desktop would cap at 60 + ellipsis
  });
});
