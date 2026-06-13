import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SidechainGroup, subagentSummaryLabel } from './SidechainGroup';
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
