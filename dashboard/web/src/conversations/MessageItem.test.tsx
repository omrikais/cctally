import { createRef } from 'react';
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MessageItem } from './MessageItem';
import type { ConversationItem } from '../types/conversation';

const human: ConversationItem = {
  kind: 'human',
  anchor: { session_id: 's', uuid: 'h1', id: 1 },
  member_uuids: ['h1'],
  ts: 't',
  text: 'hello there',
  blocks: [],
  is_sidechain: false,
  subagent_key: null,
  parent_uuid: null,
};

const assistant: ConversationItem = {
  kind: 'assistant',
  anchor: { session_id: 's', uuid: 'a1', id: 2 },
  member_uuids: ['a1'],
  ts: 't',
  text: 'hi back',
  blocks: [{ kind: 'thinking', text: 'pondering' }],
  model: 'claude-opus-4',
  is_sidechain: false,
  subagent_key: null,
  parent_uuid: null,
  cost_usd: 0.1234,
};

const toolResult: ConversationItem = {
  kind: 'tool_result',
  anchor: { session_id: 's', uuid: 'tr1', id: 3 },
  member_uuids: ['tr1'],
  ts: 't',
  text: '',
  blocks: [{ kind: 'tool_result', text: 'output', truncated: false, is_error: false }],
  is_sidechain: false,
  subagent_key: null,
  parent_uuid: null,
};

describe('MessageItem', () => {
  it('renders a human message with prose and the data-uuid', () => {
    const { container } = render(<MessageItem item={human} />);
    const root = container.querySelector('.conv-item--human')!;
    expect(root).not.toBeNull();
    expect(root.getAttribute('data-uuid')).toBe('h1');
    expect(container.textContent).toContain('hello there');
    expect(container.textContent).toContain('You');
  });

  it('renders an assistant message with model badge, prose, blocks, and cost', () => {
    const { container } = render(<MessageItem item={assistant} />);
    const root = container.querySelector('.conv-item--assistant')!;
    expect(root).not.toBeNull();
    expect(container.querySelector('.conv-item-model')!.textContent).toBe('claude-opus-4');
    expect(container.textContent).toContain('hi back');
    expect(container.querySelector('details.conv-chip--thinking')).not.toBeNull();
    const cost = container.querySelectorAll('.conv-item-cost');
    expect(cost).toHaveLength(1);
    expect(cost[0].textContent).toBe('$0.1234');
  });

  it('renders the assistant cost EXACTLY ONCE', () => {
    const { container } = render(<MessageItem item={assistant} />);
    expect(container.querySelectorAll('.conv-item-cost')).toHaveLength(1);
  });

  it('omits the cost footer on a null-msg_id assistant (no cost_usd)', () => {
    const nullMsg: ConversationItem = {
      kind: 'assistant',
      anchor: { session_id: 's', uuid: 'a2', id: 4 },
      member_uuids: ['a2'],
      ts: 't',
      text: 'partial',
      blocks: [],
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      // model present, cost_usd absent (the null-msg case)
      model: 'claude-opus-4',
    };
    const { container } = render(<MessageItem item={nullMsg} />);
    expect(container.querySelectorAll('.conv-item-cost')).toHaveLength(0);
    expect(container.querySelector('.conv-item-model')!.textContent).toBe('claude-opus-4');
  });

  it('renders a tool_result item as a single collapsed disclosure', () => {
    const { container } = render(<MessageItem item={toolResult} />);
    const root = container.querySelector('.conv-item--tool_result')!;
    expect(root).not.toBeNull();
    expect(container.querySelector('details.conv-chip--result summary')!.textContent).toContain('Tool result');
    expect(container.textContent).toContain('output');
  });

  it('forwards a ref to the container div', () => {
    const ref = createRef<HTMLDivElement>();
    render(<MessageItem ref={ref} item={human} />);
    expect(ref.current).not.toBeNull();
    expect(ref.current!.classList.contains('conv-item--human')).toBe(true);
    expect(ref.current!.getAttribute('data-uuid')).toBe('h1');
  });
});
