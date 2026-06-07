import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
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
});
