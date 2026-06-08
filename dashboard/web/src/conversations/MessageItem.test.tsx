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

  it('folds a system-marker human turn into an expandable pill (raw text preserved)', () => {
    const marker: ConversationItem = {
      kind: 'human',
      anchor: { session_id: 's', uuid: 'm1', id: 10 },
      member_uuids: ['m1'],
      ts: 't',
      text: '<command-name>clear</command-name>',
      blocks: [],
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
    };
    const { container } = render(<MessageItem item={marker} />);
    // Folded, not a normal human prose turn.
    expect(container.querySelector('.conv-item--human')).toBeNull();
    const pill = container.querySelector('.conv-item--system details.conv-system-marker');
    expect(pill).not.toBeNull();
    // data-uuid preserved on the container (the #160 jump relies on it).
    expect(container.querySelector('[data-uuid="m1"]')).not.toBeNull();
    // Raw text is reachable (never destroyed) — it is in the DOM, inside the disclosure.
    expect(container.textContent).toContain('<command-name>clear</command-name>');
  });

  it('folds even when item.blocks carries a {kind:"text"} block (the real human payload)', () => {
    const marker: ConversationItem = {
      kind: 'human',
      anchor: { session_id: 's', uuid: 'm2', id: 11 },
      member_uuids: ['m2'],
      ts: 't',
      text: '<command-name>clear</command-name>',
      // human prose arrives BOTH as text AND as a text block — the guard must
      // be "no NON-text blocks", not "blocks.length === 0".
      blocks: [{ kind: 'text', text: '<command-name>clear</command-name>' }],
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
    };
    const { container } = render(<MessageItem item={marker} />);
    expect(container.querySelector('.conv-item--system')).not.toBeNull();
    expect(container.querySelector('.conv-item--human')).toBeNull();
  });

  it('does NOT fold a marker turn that also has a non-text block', () => {
    const withTool: ConversationItem = {
      kind: 'human',
      anchor: { session_id: 's', uuid: 'm3', id: 12 },
      member_uuids: ['m3'],
      ts: 't',
      text: '<command-name>clear</command-name>',
      blocks: [{ kind: 'image', media_type: 'image/png', bytes: 10 }],
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
    };
    const { container } = render(<MessageItem item={withTool} />);
    expect(container.querySelector('.conv-item--system')).toBeNull();
    expect(container.querySelector('.conv-item--human')).not.toBeNull();
  });

  it('does NOT fold an ordinary human turn', () => {
    const { container } = render(<MessageItem item={human} />);
    expect(container.querySelector('.conv-item--system')).toBeNull();
    expect(container.querySelector('.conv-item--human')).not.toBeNull();
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

  it('omits the cost footer when cost_usd is present but 0.0 (the real backend null-msg payload)', () => {
    // _build_simple emits cost_usd: 0.0 for an assistant-with-null-msg_id, so
    // the field is PRESENT (not absent as the sibling test above hand-builds).
    // A bare `typeof === 'number'` would render a misleading "$0.0000"; the
    // `> 0` gate must drop the footer for this no-attributable-cost sentinel.
    const zeroCost: ConversationItem = {
      kind: 'assistant',
      anchor: { session_id: 's', uuid: 'a3', id: 5 },
      member_uuids: ['a3'],
      ts: 't',
      text: 'partial',
      blocks: [],
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      model: 'claude-opus-4',
      cost_usd: 0.0,
    };
    const { container } = render(<MessageItem item={zeroCost} />);
    expect(container.querySelectorAll('.conv-item-cost')).toHaveLength(0);
    expect(container.textContent).not.toContain('$0.0000');
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
