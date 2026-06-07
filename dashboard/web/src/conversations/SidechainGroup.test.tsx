import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SidechainGroup } from './SidechainGroup';
import type { ConversationItem } from '../types/conversation';

function member(uuid: string): ConversationItem {
  return { kind: 'human', anchor: { session_id: 's', uuid, id: 0 }, member_uuids: [uuid], ts: 't', text: uuid, blocks: [], is_sidechain: true };
}

describe('SidechainGroup', () => {
  it('collapses N members behind a closed disclosure with the count in the summary', () => {
    const items = [member('s1'), member('s2'), member('s3')];
    const { container } = render(<SidechainGroup items={items} />);
    const details = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(details).not.toBeNull();
    // Collapsed by default (not open).
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')!.textContent).toContain('Subagent thread');
    expect(details.querySelector('summary')!.textContent).toContain('3 messages');
  });

  it('renders each member as a MessageItem inside the body', () => {
    const items = [member('s1'), member('s2')];
    const { container } = render(<SidechainGroup items={items} />);
    expect(container.querySelectorAll('.conv-sidechain-body .conv-item')).toHaveLength(2);
    expect(container.querySelector('[data-uuid="s1"]')).not.toBeNull();
    expect(container.querySelector('[data-uuid="s2"]')).not.toBeNull();
  });
});
