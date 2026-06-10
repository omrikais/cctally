import { createRef } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
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

// The assistant turn renders its prose from text BLOCKS in document order (the
// joined `item.text` is no longer rendered separately) — so the prose arrives
// as a {kind:'text'} block, matching the real backend payload.
const assistant: ConversationItem = {
  kind: 'assistant',
  anchor: { session_id: 's', uuid: 'a1', id: 2 },
  member_uuids: ['a1'],
  ts: 't',
  text: 'hi back',
  blocks: [
    { kind: 'thinking', text: 'pondering' },
    { kind: 'text', text: 'hi back' },
  ],
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
    // C3/H2: the system-marker summary now shows an SVG (not ⚙) and the chevron
    // it previously lacked.
    const summary = pill!.querySelector('summary')!;
    expect(summary.textContent).toContain('System marker');
    expect(summary.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(summary.querySelector('.conv-chev')).not.toBeNull();
    expect(summary.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
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
    const summary = container.querySelector('details.conv-chip--result summary')!;
    expect(summary.textContent).toContain('Tool result');
    // C3/H2: an inline SVG instead of the 📤 emoji, and the chevron the
    // top-level tool_result disclosure was previously missing.
    expect(summary.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(summary.querySelector('.conv-chev')).not.toBeNull();
    expect(summary.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
    expect(container.textContent).toContain('output');
  });

  it('forwards a ref to the container div', () => {
    const ref = createRef<HTMLDivElement>();
    render(<MessageItem ref={ref} item={human} />);
    expect(ref.current).not.toBeNull();
    expect(ref.current!.classList.contains('conv-item--human')).toBe(true);
    expect(ref.current!.getAttribute('data-uuid')).toBe('h1');
  });

  it('assistant renders blocks in document order (prose, then its tool run) (#164)', () => {
    render(
      <MessageItem
        item={
          {
            kind: 'assistant',
            anchor: { session_id: 's', uuid: 'a1', id: 1 },
            member_uuids: ['a1'],
            ts: '',
            text: 'Reading the spec.',
            model: 'claude-opus-4-8',
            is_sidechain: false,
            subagent_key: null,
            parent_uuid: null,
            cost_usd: 0,
            blocks: [
              { kind: 'text', text: 'Reading the spec.' },
              {
                kind: 'tool_call',
                name: 'Read',
                input_summary: '{}',
                preview: '/spec.md',
                tool_use_id: 't1',
                result: { text: 'BODY', truncated: false, is_error: false },
              },
            ],
          } as ConversationItem
        }
      />,
    );
    expect(screen.getByText('Reading the spec.')).toBeInTheDocument();
    expect(screen.getByText('/spec.md')).toBeInTheDocument();
  });

  it('assistant does NOT double-render prose (text block + joined item.text)', () => {
    const { container } = render(
      <MessageItem
        item={
          {
            kind: 'assistant',
            anchor: { session_id: 's', uuid: 'a9', id: 9 },
            member_uuids: ['a9'],
            ts: '',
            text: 'unique-prose-token',
            model: 'claude-opus-4-8',
            is_sidechain: false,
            subagent_key: null,
            parent_uuid: null,
            cost_usd: 0,
            blocks: [{ kind: 'text', text: 'unique-prose-token' }],
          } as ConversationItem
        }
      />,
    );
    // Exactly one Markdown render of the prose (not duplicated by a separate
    // item.text render alongside the block walk).
    expect(container.querySelectorAll('.md')).toHaveLength(1);
  });

  it('human turn unchanged: system-marker fold still fires (keys on item.text)', () => {
    render(
      <MessageItem
        item={
          {
            kind: 'human',
            anchor: { session_id: 's', uuid: 'h1', id: 1 },
            member_uuids: ['h1'],
            ts: '',
            text: '<command-name>/clear</command-name>',
            blocks: [{ kind: 'text', text: '<command-name>/clear</command-name>' }],
            is_sidechain: false,
            subagent_key: null,
            parent_uuid: null,
          } as ConversationItem
        }
      />,
    );
    expect(screen.getByText(/System marker/i)).toBeInTheDocument();
  });

  it('human turn does NOT double-render prose', () => {
    const { container } = render(
      <MessageItem
        item={
          {
            kind: 'human',
            anchor: { session_id: 's', uuid: 'h7', id: 7 },
            member_uuids: ['h7'],
            ts: '',
            text: 'plain human question',
            blocks: [{ kind: 'text', text: 'plain human question' }],
            is_sidechain: false,
            subagent_key: null,
            parent_uuid: null,
          } as ConversationItem
        }
      />,
    );
    expect(container.querySelector('.conv-item--human')).not.toBeNull();
    // Prose rendered once, not doubled by the block walk.
    const matches = container.textContent!.match(/plain human question/g) ?? [];
    expect(matches).toHaveLength(1);
  });
});

describe('MessageItem (message-text copy, G2)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('a prose turn renders a copy button that copies item.text', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<MessageItem item={assistant} />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith(assistant.text);
  });

  it('a human prose turn renders a copy button that copies item.text', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<MessageItem item={human} />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith(human.text);
  });

  it('a tool-only assistant turn (empty item.text) renders no message-text copy', () => {
    const toolOnly: ConversationItem = {
      kind: 'assistant',
      anchor: { session_id: 's', uuid: 'ao1', id: 20 },
      member_uuids: ['ao1'],
      ts: 't',
      text: '',
      blocks: [
        {
          kind: 'tool_call',
          name: 'Read',
          input_summary: '{}',
          preview: '/x',
          tool_use_id: 't1',
          result: { text: 'X', truncated: false, is_error: false },
        },
      ],
      model: 'claude-opus-4',
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      cost_usd: 0,
    };
    const { container } = render(<MessageItem item={toolOnly} />);
    // No .conv-item-actions wrapper (the collapsed chip is closed, so no I/O
    // copy buttons either) → zero copy buttons at the message level.
    expect(container.querySelector('.conv-item-actions')).toBeNull();
  });

  // ---- meta (injected isMeta content) ----------------------------------
  const metaSkill: ConversationItem = {
    kind: 'meta',
    anchor: { session_id: 's', uuid: 'm1', id: 9 },
    member_uuids: ['m1'],
    ts: 't',
    text: 'Base directory for this skill: /x/skills/brainstorming\n\n# Brainstorming Ideas',
    blocks: [{ kind: 'text', text: 'Base directory for this skill: /x/skills/brainstorming\n\n# Brainstorming Ideas' }],
    is_sidechain: false,
    subagent_key: null,
    parent_uuid: null,
    meta_kind: 'skill',
    skill_name: 'brainstorming',
  };

  it('renders a skill meta row as a collapsed disclosure, NEVER a "You" prompt', () => {
    const { container } = render(<MessageItem item={metaSkill} />);
    expect(container.querySelector('.conv-item--human')).toBeNull();
    expect(container.textContent).not.toContain('You');
    const details = container.querySelector('details.conv-meta.conv-meta--skill')!;
    expect(details).not.toBeNull();
    expect((details as HTMLDetailsElement).open).toBe(false); // collapsed by default
    expect(container.querySelector('.conv-item--meta')!.getAttribute('data-uuid')).toBe('m1');
    expect(container.textContent).toContain('Skill content');
    expect(container.textContent).toContain('brainstorming'); // the skill name
    // body renders the markdown (the heading) inside the disclosure
    expect(container.textContent).toContain('Brainstorming Ideas');
  });

  it('renders a skill meta row WITHOUT a skill_name as a name-less pill', () => {
    const noName: ConversationItem = { ...metaSkill, anchor: { session_id: 's', uuid: 'm1b', id: 10 }, skill_name: null };
    const { container } = render(<MessageItem item={noName} />);
    expect(container.textContent).toContain('Skill content');
    expect(container.querySelector('.conv-meta-name')).toBeNull();
  });

  it('renders a command meta row as the System marker pill (raw <pre>, not markdown)', () => {
    const metaCommand: ConversationItem = {
      kind: 'meta',
      anchor: { session_id: 's', uuid: 'm2', id: 11 },
      member_uuids: ['m2'],
      ts: 't',
      text: '<command-name>clear</command-name>',
      blocks: [{ kind: 'text', text: '<command-name>clear</command-name>' }],
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      meta_kind: 'command',
      skill_name: null,
    };
    const { container } = render(<MessageItem item={metaCommand} />);
    expect(container.querySelector('.conv-item--human')).toBeNull();
    expect(container.querySelector('details.conv-meta--command')).not.toBeNull();
    expect(container.querySelector('pre.conv-meta-body--pre')!.textContent).toContain('<command-name>clear</command-name>');
  });

  it('renders a context meta row as an Injected context pill with a preview', () => {
    const metaContext: ConversationItem = {
      kind: 'meta',
      anchor: { session_id: 's', uuid: 'm3', id: 12 },
      member_uuids: ['m3'],
      ts: 't',
      text: '## Git Context\n- branch: main',
      blocks: [{ kind: 'text', text: '## Git Context\n- branch: main' }],
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      meta_kind: 'context',
      skill_name: null,
    };
    const { container } = render(<MessageItem item={metaContext} />);
    expect(container.querySelector('.conv-item--human')).toBeNull();
    expect(container.querySelector('details.conv-meta--context')).not.toBeNull();
    expect(container.textContent).toContain('Injected context');
    expect(container.querySelector('.conv-meta-preview')!.textContent).toContain('## Git Context');
  });

  it('gives a meta row no spine role-dot class (--human/--assistant only)', () => {
    const { container } = render(<MessageItem item={metaSkill} />);
    expect(container.querySelector('.conv-item--human')).toBeNull();
    expect(container.querySelector('.conv-item--assistant')).toBeNull();
    expect(container.querySelector('.conv-item--meta')).not.toBeNull();
  });
});
