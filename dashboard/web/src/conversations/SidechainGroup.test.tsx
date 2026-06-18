import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SidechainGroup, subagentSummaryLabel } from './SidechainGroup';
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
    const { container } = render(<SidechainGroup subagentKey="k1" items={items} nested={false} />);
    const details = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(details).not.toBeNull();
    expect(details.open).toBe(false);
    expect(details.classList.contains('conv-sidechain--nested')).toBe(false);
    const summary = details.querySelector('summary')!.textContent!;
    expect(summary).toContain('Audit module A'); // label from first member prose
    expect(summary).toContain('2 msgs');
    expect(summary).toContain('$0.42');           // 0.30 + 0.12
  });

  it('applies the nested class when nested', () => {
    const { container } = render(<SidechainGroup subagentKey="k1" items={[member('s1')]} nested={true} />);
    expect(container.querySelector('details.conv-sidechain')!.classList.contains('conv-sidechain--nested')).toBe(true);
  });

  it('renders each member as a MessageItem in the body', () => {
    const { container } = render(<SidechainGroup subagentKey="k1" items={[member('s1'), member('s2')]} nested={false} />);
    expect(container.querySelectorAll('.conv-sidechain-body .conv-item')).toHaveLength(2);
    expect(container.querySelector('[data-uuid="s1"]')).not.toBeNull();
  });

  it('renders the card header: glyph, static Subagent eyebrow, serif title, meta', () => {
    const items = [
      member('r', { kind: 'assistant', text: 'Audit module A', model: 'claude-opus-4', cost_usd: 0.30 } as Partial<ConversationItem>),
      member('s2', { kind: 'assistant', text: '', model: 'claude-opus-4', cost_usd: 0.12 } as Partial<ConversationItem>),
    ];
    render(<SidechainGroup subagentKey="aaaa1111" items={items} nested={false} />);
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
    render(<SidechainGroup subagentKey="aaaa1111" items={items} nested={false}
             meta={{ kind: 'Explore', total_tokens: 23285, total_duration_ms: 10668,
                     total_tool_use_count: 1, status: 'completed' }} />);
    expect(document.querySelector('.conv-sidechain-kindname')!.textContent).toContain('Explore');
  });

  it('renders the toolUseResult meta line', () => {
    const items = [member('r', { kind: 'assistant', text: 'Audit module A', cost_usd: 0.30 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="aaaa1111" items={items} nested={false}
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
    render(<SidechainGroup subagentKey="bbbb2222" items={items} nested={false}
             meta={{ kind: 'code-reviewer', status: 'error' }} />);
    const err = document.querySelector('.conv-subagent-err')!;
    expect(err.textContent).toContain('error');
  });

  it('spells out a non-completed terminal status (⚠ <status>) with the warn class', () => {
    const items = [member('r', { kind: 'assistant', text: 'Audit module D', cost_usd: 0.30 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="dddd4444" items={items} nested={false}
             meta={{ kind: 'Explore', status: 'aborted' }} />);
    const warn = document.querySelector('.conv-subagent-warn')!;
    expect(warn.textContent).toContain('aborted');
    expect(warn.textContent).toContain('⚠');
  });

  it('renders the kind eyebrow but no submeta line when only kind is present (no-blank-line guard)', () => {
    const items = [member('r', { kind: 'assistant', text: 'Audit module E', cost_usd: 0.30 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="eeee5555" items={items} nested={false}
             meta={{ kind: 'Explore' }} />);
    expect(document.querySelector('.conv-sidechain-kindname')!.textContent).toContain('Explore');
    expect(document.querySelector('.conv-sidechain-submeta')).toBeNull();
  });

  it('falls back to title-only when meta is absent (no kind, no submeta line)', () => {
    const items = [member('r', { kind: 'assistant', text: 'Audit module C', cost_usd: 0.30 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="cccc3333" items={items} nested={false} />);
    expect(document.querySelector('.conv-sidechain-kindname')).toBeNull();
    expect(document.querySelector('.conv-sidechain-submeta')).toBeNull();
    expect(document.querySelector('.conv-sidechain-kind')!.textContent).toBe('Subagent');
  });

  it('pluralizes the tool count (0 tools / 2 tools)', () => {
    const items = [member('r', { kind: 'assistant', text: 'x', cost_usd: 0 } as Partial<ConversationItem>)];
    const { rerender } = render(<SidechainGroup subagentKey="k" items={items} nested={false}
             meta={{ kind: 'Explore', total_tool_use_count: 0 }} />);
    expect(document.querySelector('.conv-sidechain-submeta')!.textContent).toContain('0 tools');
    rerender(<SidechainGroup subagentKey="k" items={items} nested={false}
             meta={{ kind: 'Explore', total_tool_use_count: 2 }} />);
    expect(document.querySelector('.conv-sidechain-submeta')!.textContent).toContain('2 tools');
  });

  it('applies conv-sidechain--force only while forceOpen (G1 §4a #160 instant-open)', () => {
    const items = [member('s1'), member('s2')];
    const { container, rerender } = render(
      <SidechainGroup subagentKey="k1" items={items} nested={false} forceOpen={false} />,
    );
    const details = container.querySelector('details.conv-sidechain')!;
    expect(details).not.toHaveClass('conv-sidechain--force');
    rerender(<SidechainGroup subagentKey="k1" items={items} nested={false} forceOpen={true} />);
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
      <SidechainGroup subagentKey="k1" items={items} nested={false} rootUuid="root" getCardRef={getCardRef} />,
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
    const base = { subagentKey: 'k1', items, nested: false, rootUuid: 'root', getCardRef };
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
      <SidechainGroup subagentKey="k1" items={items} nested={false} onOpenChange={onOpenChange} />,
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

  it('fires onOpenChange(subagentKey, true) when a force-open latches', () => {
    const onOpenChange = vi.fn();
    const items = [member('s1'), member('s2')];
    const base = { subagentKey: 'k1', items, nested: false, onOpenChange };
    const { rerender } = render(<SidechainGroup {...base} forceOpen={false} />);
    expect(onOpenChange).not.toHaveBeenCalled();
    // Force the thread open: the latch effect must report the key as open so the
    // reader counts a subsequent append into THIS now-visible thread.
    rerender(<SidechainGroup {...base} forceOpen={true} />);
    expect(onOpenChange).toHaveBeenCalledWith('k1', true);
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
    const base = { subagentKey: 'k1', items, nested: false, getItemRef };

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
        nested={true}
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
      <SidechainGroup subagentKey="cc110001" items={childItems} nested={true}
        forceOpen={false} children={[grandchild]} depth={0} childCtx={{}} />,
    );
    // Collapsed parent → exactly one card (no grandchild rendered yet).
    expect(container.querySelectorAll('details.conv-sidechain')).toHaveLength(1);
    expect(container.querySelector('[data-uuid="g1"]')).toBeNull();
  });

  it('renders the "~" derived-totals affordance when meta.totals_derived is true', () => {
    const items = [member('r', { kind: 'assistant', text: 'Background audit', cost_usd: 0.1 } as Partial<ConversationItem>)];
    render(<SidechainGroup subagentKey="a55c0003" items={items} nested={false}
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
    render(<SidechainGroup subagentKey="cc110001" items={items} nested={false}
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
      <SidechainGroup subagentKey="par" items={childItems} nested={false}
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
    render(<SidechainGroup subagentKey="abc" items={items} nested={false}
      meta={{ kind: 'general-purpose', description: 'Code review Phase A' }} />);
    expect(screen.getByText('Code review Phase A')).toBeTruthy();
    // The first-prompt label must NOT win when a description is present.
    expect(document.querySelector('.conv-sidechain-title')!.textContent).toBe('Code review Phase A');
  });

  it('falls back to the first-prompt label when no description', () => {
    const items = [member('u1', { kind: 'human', text: 'Do the analysis' })];
    render(<SidechainGroup subagentKey="abc" items={items} nested={false}
      meta={{ kind: 'general-purpose' }} />);
    // "Do the analysis" also appears in the rendered member body, so scope to
    // the title span — the label fallback must still drive the header title.
    expect(document.querySelector('.conv-sidechain-title')!.textContent).toBe('Do the analysis');
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
    const { container } = render(<SidechainGroup subagentKey="k1" items={items} nested={false} isMobile />);
    const model = container.querySelector('.conv-sidechain-model')!.textContent!;
    expect(model).toBe('opus-4-8');                 // both collapse to one
    expect(model).not.toContain('claude-');
    expect(model).not.toContain('20251101');
  });

  it('renders the full ids on desktop (isMobile false)', () => {
    const { container } = render(<SidechainGroup subagentKey="k1" items={items} nested={false} isMobile={false} />);
    const model = container.querySelector('.conv-sidechain-model')!.textContent!;
    expect(model).toContain('claude-opus-4-8-20251101');
    expect(model).toContain('claude-opus-4-8');
  });

  it('shows >60 chars of a no-meta fallback title on mobile (MOBILE_LABEL_MAX path)', () => {
    const long = 'Implement the conversation reader find bar and wire up the n and N step bindings cleanly';
    const longItems = [member('r', { kind: 'assistant', text: long, cost_usd: 0 } as Partial<ConversationItem>)];
    const { container } = render(<SidechainGroup subagentKey="k1" items={longItems} nested={false} isMobile />);
    const title = container.querySelector('.conv-sidechain-title')!.textContent!;
    expect(title.length).toBeGreaterThan(61); // desktop would cap at 60 + ellipsis
  });
});
