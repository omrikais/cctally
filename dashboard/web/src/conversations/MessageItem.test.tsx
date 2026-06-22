import { createRef } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageItem } from './MessageItem';
import { TranscriptContext } from './TranscriptContext';
import type { FmtCtx } from '../lib/fmt';
import type { ConversationItem } from '../types/conversation';

// #184 — eyebrow times read the display-tz FmtCtx from TranscriptContext (the
// reader provides it once; MessageItem no longer subscribes per-item via
// useDisplayTz). A mutable holder lets a test flip the tz without re-rendering
// the provider tree by hand. Default is Etc/UTC so the bulk of the existing
// tests render a deterministic clock. The direct `render(<MessageItem .../>)`
// calls below rely on the context DEFAULT (also Etc/UTC); only the tz-sensitive
// cases wrap in the provider via `renderWithTz`.
const fmtCtx: FmtCtx = { tz: 'Etc/UTC', offsetLabel: 'UTC' };
function renderWithTz(item: ConversationItem) {
  return render(
    <TranscriptContext.Provider value={{ sessionId: 's', fmtCtx }}>
      <MessageItem item={item} />
    </TranscriptContext.Provider>,
  );
}

// cache-failure-markers spec §3 — the chip's `markersEnabled` rides on
// TranscriptContext (like focusMode/fmtCtx); the reader provides it from
// selectMarkersEnabled. Default ON, so a bare render shows the chip.
function renderWithMarkers(item: ConversationItem, markersEnabled: boolean) {
  return render(
    <TranscriptContext.Provider value={{ sessionId: 's', fmtCtx, markersEnabled }}>
      <MessageItem item={item} />
    </TranscriptContext.Provider>,
  );
}

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

const systemMarker: ConversationItem = {
  kind: 'human',
  anchor: { session_id: 's', uuid: 'sm1', id: 4 },
  member_uuids: ['sm1'],
  ts: 't',
  text: '<command-name>clear</command-name>',
  blocks: [],
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

  it('renders an assistant message with the model chip, prose, blocks, and cost', () => {
    const { container } = render(<MessageItem item={assistant} />);
    const root = container.querySelector('.conv-item--assistant')!;
    expect(root).not.toBeNull();
    // #175 F3: the model is rendered through the shared .chip system (opus
    // here), not the old plain .conv-item-model text.
    const chip = container.querySelector('.conv-item-head .chip')!;
    expect(chip.textContent).toBe('claude-opus-4');
    expect(chip.classList.contains('opus')).toBe(true);
    expect(container.querySelector('.conv-item-model')).toBeNull();
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

  // #217 S6 F3 — the per-turn cost micro-bar. Width/intensity rides a CSS var
  // (--conv-cost-frac) carrying the cost/maxTurnCost ratio so the JSDOM test can
  // assert it without layout. The denominator (session max-turn-cost) is provided
  // by the reader via TranscriptContext.maxTurnCost.
  it('renders a per-turn cost bar sized to cost / maxTurnCost', () => {
    const item: ConversationItem = {
      kind: 'assistant',
      anchor: { session_id: 's1', uuid: 'a1', id: 1 },
      member_uuids: ['a1'],
      ts: '2026-06-22T00:00:00Z',
      text: 'hi',
      blocks: [{ kind: 'text', text: 'hi' }],
      model: 'claude-opus-4',
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      cost_usd: 0.02,
    };
    const { container } = render(
      <TranscriptContext.Provider value={{ sessionId: 's1', maxTurnCost: 0.08 }}>
        <MessageItem item={item} />
      </TranscriptContext.Provider>,
    );
    const bar = container.querySelector('.conv-cost-bar') as HTMLElement;
    expect(bar).toBeTruthy();
    // 0.02 / 0.08 = 0.25 → the fraction var encodes 0.25 exactly. Assert the
    // exact value (not a loose substring — #226), matching the `.toBe('1')`
    // sub-cent-at-max case below.
    expect(bar.style.getPropertyValue('--conv-cost-frac')).toBe('0.25');
    // Decorative beyond the precise $-figure in the footer text.
    expect(bar.getAttribute('aria-hidden')).toBe('true');
  });

  // #217 S6 F3 — a SUB-CENT turn that is the session max still reads as a FULL
  // bar: intensity tracks the RELATIVE ratio (cost/sessionMaxTurnCost), not the
  // absolute costClass (which would bin every sub-cent turn into cost-xs). This is
  // the Codex P2 correction made non-vacuous: frac is 1.0 here despite a < $0.01
  // absolute cost.
  it('drives bar intensity from the ratio, not absolute cost (sub-cent max → full bar)', () => {
    const item: ConversationItem = {
      kind: 'assistant',
      anchor: { session_id: 's1', uuid: 'a1', id: 1 },
      member_uuids: ['a1'],
      ts: '2026-06-22T00:00:00Z',
      text: 'hi',
      blocks: [{ kind: 'text', text: 'hi' }],
      model: 'claude-opus-4',
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      cost_usd: 0.004,
    };
    const { container } = render(
      <TranscriptContext.Provider value={{ sessionId: 's1', maxTurnCost: 0.004 }}>
        <MessageItem item={item} />
      </TranscriptContext.Provider>,
    );
    const bar = container.querySelector('.conv-cost-bar') as HTMLElement;
    expect(bar).toBeTruthy();
    expect(bar.style.getPropertyValue('--conv-cost-frac')).toBe('1');
  });

  it('renders no cost bar when maxTurnCost is 0 (no positive-cost turn loaded)', () => {
    // Provider value omits maxTurnCost → defaults 0 → costIntensity 0 → no bar.
    const { container } = render(
      <TranscriptContext.Provider value={{ sessionId: 's1' }}>
        <MessageItem item={assistant} />
      </TranscriptContext.Provider>,
    );
    expect(container.querySelector('.conv-cost-bar')).toBeNull();
  });

  it('renders the assistant model as a .chip with the modelChipClass (#175 F3)', () => {
    const opusItem: ConversationItem = { ...assistant, model: 'claude-opus-4-8' };
    const { container } = render(<MessageItem item={opusItem} />);
    const chip = container.querySelector('.conv-item-head .chip');
    expect(chip?.textContent).toBe('claude-opus-4-8');
    expect(chip?.classList.contains('opus')).toBe(true);
    expect(container.querySelector('.conv-item-model')).toBeNull();
  });

  it('renders no chip and no em dash for a null-model assistant (#175 F3)', () => {
    const nullModel: ConversationItem = {
      kind: 'assistant',
      anchor: { session_id: 's', uuid: 'an', id: 30 },
      member_uuids: ['an'],
      ts: 't',
      text: 'partial',
      blocks: [{ kind: 'text', text: 'partial' }],
      model: null,
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      cost_usd: 0,
    };
    const { container } = render(<MessageItem item={nullModel} />);
    expect(container.querySelector('.conv-item-head .chip')).toBeNull();
    expect(container.querySelector('.conv-item-head')?.textContent).not.toContain('—');
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
    expect(container.querySelector('.conv-item-head .chip')!.textContent).toBe('claude-opus-4');
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

  it('assistant & human heads are plain divs, NOT click-to-scroll buttons (#176 revert)', () => {
    // #176 reverted the #175 sticky/click-to-top head: the head is a plain
    // <div className="conv-item-head"> again — not a <button> — and clicking it
    // scrolls nothing (the floating "↑ Top of turn" button on the reader owns
    // jump-to-start now).
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    const assistantTurn: ConversationItem = {
      kind: 'assistant',
      anchor: { session_id: 's', uuid: 'aJ', id: 40 },
      member_uuids: ['aJ'],
      ts: 't',
      text: 'hi',
      blocks: [{ kind: 'text', text: 'hi' }],
      model: 'claude-sonnet-4-6',
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      cost_usd: 0,
    };
    for (const item of [assistantTurn, human]) {
      const { container, unmount } = render(<MessageItem item={item} />);
      const head = container.querySelector('.conv-item-head')!;
      expect(head).not.toBeNull();
      // Plain div, not a button.
      expect(head.tagName).toBe('DIV');
      expect(container.querySelector('button.conv-item-head')).toBeNull();
      // No click-to-scroll affordance on the head.
      fireEvent.click(head);
      expect(scrollSpy).not.toHaveBeenCalled();
      unmount();
    }
    scrollSpy.mockRestore();
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

  // #188 — a promoted slash-command turn: kind='human', text=args,
  // command_name set, blocks still hold the raw <command-*> plumbing (in a lone
  // text block, which the human branch filters out of the MessageBlocks walk).
  const promotedCommand: ConversationItem = {
    kind: 'human',
    anchor: { session_id: 's', uuid: 'pc1', id: 50 },
    member_uuids: ['pc1'],
    ts: 't',
    text: 'do X',
    command_name: '/frontend-design',
    blocks: [
      {
        kind: 'text',
        text:
          '<command-name>/frontend-design</command-name>' +
          '<command-args>do X</command-args>',
      },
    ],
    is_sidechain: false,
    subagent_key: null,
    parent_uuid: null,
  };

  it('renders a promoted command as a You bubble with a badge, no raw plumbing', () => {
    const { container } = render(<MessageItem item={promotedCommand} />);
    // A normal human prose turn (NOT the system-marker pill, NOT a meta pill).
    expect(container.querySelector('.conv-item--human')).not.toBeNull();
    expect(container.querySelector('.conv-item--system')).toBeNull();
    expect(container.textContent).toContain('You');
    // The args render as prose.
    expect(screen.getByText('do X')).toBeInTheDocument();
    // The command name renders as a compact badge.
    const badge = container.querySelector('.conv-cmd-badge')!;
    expect(badge).not.toBeNull();
    expect(badge.textContent).toContain('/frontend-design');
    // The raw <command-*> plumbing must NOT leak into the rendered body.
    expect(container.textContent).not.toContain('<command-name>');
    expect(container.textContent).not.toContain('<command-args>');
  });

  it('renders no badge for a plain human turn (command_name absent)', () => {
    const { container } = render(<MessageItem item={human} />);
    expect(container.querySelector('.conv-cmd-badge')).toBeNull();
  });

  it('a promoted command turn still copies the args (not the plumbing)', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<MessageItem item={promotedCommand} />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith('do X');
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

  it('a prose turn renders both a permalink and a copy button', () => {
    render(<MessageItem item={assistant} />);
    expect(
      screen.getByRole('button', { name: 'Copy link to this turn' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument();
  });

  it('the permalink button copies the deep-link for the turn', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<MessageItem item={assistant} />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy link to this turn' }));
    // assistant.anchor = { session_id: 's', uuid: 'a1', id: 2 }. The origin is
    // derived from the runtime so the assertion is robust to jsdom's default host.
    expect(writeText).toHaveBeenCalledWith(
      `${window.location.origin}/#/conversations/s/a1`,
    );
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

  it('unpaired skill body (SessionStart / fold fallback) still renders the standalone "Skill content" pill', () => {
    // Regression guard for skill-content nesting: paired skills fold into the
    // Skill tool chip (in MessageBlocks), but an UNPAIRED skill body — a
    // standalone meta_kind:'skill' ITEM the kernel could not fold (SessionStart
    // injection, or the pre-reingest NULL-column window) — must keep rendering
    // the standalone collapsed pill here.
    const { container } = render(<MessageItem item={metaSkill} />);
    const details = container.querySelector('details.conv-meta.conv-meta--skill')!;
    expect(details).not.toBeNull();
    expect((details as HTMLDetailsElement).open).toBe(false);
    expect(container.textContent).toContain('Skill content');
    expect(container.textContent).toContain('brainstorming');
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

  // #217 S5 F6 — a context body carrying an unfenced git diff routes the diff
  // region through UnifiedDiffView (real diff rows), while the prose lead stays
  // Markdown. A context body with NO `diff --git` marker stays pure prose.
  it('routes an injected git-context diff body through UnifiedDiffView', () => {
    const body =
      '- Unstaged changes: diff --git a/CLAUDE.md b/CLAUDE.md\n' +
      'index a..b 100644\n' +
      '--- a/CLAUDE.md\n' +
      '+++ b/CLAUDE.md\n' +
      '@@ -1,2 +1,3 @@\n' +
      ' ctx\n' +
      '+added\n' +
      '-removed\n';
    const metaDiff: ConversationItem = {
      kind: 'meta',
      anchor: { session_id: 's', uuid: 'mD', id: 13 },
      member_uuids: ['mD'],
      ts: 't',
      text: body,
      blocks: [{ kind: 'text', text: body }],
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      meta_kind: 'context',
      skill_name: null,
    };
    const { container } = render(<MessageItem item={metaDiff} />);
    expect(container.querySelector('.conv-ctx-diff')).not.toBeNull();
    expect(container.querySelector('.conv-diff-row--add')).not.toBeNull();
    expect(container.querySelector('.conv-diff-row--del')).not.toBeNull();
    expect(container.textContent).toContain('CLAUDE.md');
  });

  it('a context body with no diff marker renders as pure prose (no diff view)', () => {
    const metaProse: ConversationItem = {
      kind: 'meta',
      anchor: { session_id: 's', uuid: 'mP', id: 14 },
      member_uuids: ['mP'],
      ts: 't',
      text: '## Git Context\n- branch: main\n- a bullet\n+ another bullet',
      blocks: [{ kind: 'text', text: '## Git Context' }],
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      meta_kind: 'context',
      skill_name: null,
    };
    const { container } = render(<MessageItem item={metaProse} />);
    expect(container.querySelector('.conv-ctx-diff')).toBeNull();
    expect(container.querySelector('.conv-diff-row--add')).toBeNull();
  });
});

describe('MessageItem (#174 permalink on tool-result & system-marker chips)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('tool-result chip renders a permalink in its summary', () => {
    const { container } = render(<MessageItem item={toolResult} />);
    const summary = container.querySelector('.conv-chip--result > summary')!;
    expect(summary).not.toBeNull();
    expect(
      summary.querySelector('button[aria-label="Copy link to this turn"]'),
    ).not.toBeNull();
  });

  it('tool-result permalink copies the deep-link for the turn', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<MessageItem item={toolResult} />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy link to this turn' }));
    // toolResult.anchor = { session_id: 's', uuid: 'tr1' }. Origin/pathname are
    // derived from the runtime so the assertion is robust to jsdom's host.
    expect(writeText).toHaveBeenCalledWith(
      `${window.location.origin}/#/conversations/s/tr1`,
    );
  });

  it('system-marker chip renders a permalink in its summary', () => {
    const { container } = render(<MessageItem item={systemMarker} />);
    const summary = container.querySelector('.conv-system-marker > summary')!;
    expect(summary).not.toBeNull();
    expect(
      summary.querySelector('button[aria-label="Copy link to this turn"]'),
    ).not.toBeNull();
  });

  it('system-marker permalink copies the deep-link for the turn', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<MessageItem item={systemMarker} />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy link to this turn' }));
    expect(writeText).toHaveBeenCalledWith(
      `${window.location.origin}/#/conversations/s/sm1`,
    );
  });

  it('clicking the summary permalink does not propagate to an ancestor (stopPropagation contains it)', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    // PermalinkButton's stopPropagation must CONTAIN the click so it never reaches
    // an ancestor handler (e.g. a row-level click-to-select). A click-spy wrapper
    // proves that directly and is non-vacuous: drop stopPropagation and the spy
    // fires. (We do NOT assert details.open: a <button> inside a <summary> never
    // toggles the <details> in any engine — the button's own activation shadows the
    // summary's, confirmed in real Chromium — so an open-state assertion would be
    // vacuous and would NOT guard PermalinkButton's behavior.)
    const onAncestorClick = vi.fn();
    render(
      <div onClick={onAncestorClick}>
        <MessageItem item={toolResult} />
      </div>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Copy link to this turn' }));
    expect(onAncestorClick).not.toHaveBeenCalled();
  });

  it('prose permalink buttons keep the exact class "conv-copy-btn" (no regression)', () => {
    render(<MessageItem item={assistant} />);
    const btn = screen.getByRole('button', { name: 'Copy link to this turn' });
    expect(btn.className).toBe('conv-copy-btn');
  });
});

// ---- #177 S5 §6 — eyebrow times + token footer --------------------------
describe('MessageItem eyebrow time (#177 S5 §6)', () => {
  afterEach(() => {
    fmtCtx.tz = 'Etc/UTC';
    fmtCtx.offsetLabel = 'UTC';
  });

  const withTs = (over: Partial<ConversationItem> & { uuid: string; kind: ConversationItem['kind'] }): ConversationItem => {
    const { uuid, kind, ...rest } = over;
    return {
      kind,
      anchor: { session_id: 's', uuid, id: 1 },
      member_uuids: [uuid],
      ts: '2026-06-12T14:02:31Z',
      text: kind === 'tool_result' ? '' : 'body',
      blocks: kind === 'tool_result' ? [{ kind: 'tool_result', text: 'out', truncated: false, is_error: false }] : [],
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      ...rest,
    } as ConversationItem;
  };

  it('renders `· 14:02` on a human head under Etc/UTC', () => {
    const { container } = render(<MessageItem item={withTs({ uuid: 'h1', kind: 'human' })} />);
    const time = container.querySelector('.conv-item-head .conv-item-time')!;
    expect(time).not.toBeNull();
    expect(time.textContent).toBe('· 14:02');
    // The tooltip carries the full precise timestamp.
    expect(time.getAttribute('title')).toBe('2026-06-12T14:02:31Z');
  });

  it('renders the eyebrow time on an assistant head', () => {
    const { container } = render(
      <MessageItem item={withTs({ uuid: 'a1', kind: 'assistant', model: 'claude-opus-4', cost_usd: 0.01 } as never)} />,
    );
    const time = container.querySelector('.conv-item-head .conv-item-time')!;
    expect(time.textContent).toBe('· 14:02');
  });

  it('renders the eyebrow time at the END of a tool_result summary line', () => {
    const { container } = render(<MessageItem item={withTs({ uuid: 'tr1', kind: 'tool_result' })} />);
    const summary = container.querySelector('details.conv-chip--result > summary')!;
    expect(summary.querySelector('.conv-item-time')!.textContent).toBe('· 14:02');
  });

  it('renders the eyebrow time at the END of a meta summary line', () => {
    const meta = withTs({
      uuid: 'm1', kind: 'meta', text: '## ctx',
      blocks: [{ kind: 'text', text: '## ctx' }], meta_kind: 'context', skill_name: null,
    } as never);
    const { container } = render(<MessageItem item={meta} />);
    const summary = container.querySelector('details.conv-meta > summary')!;
    expect(summary.querySelector('.conv-item-time')!.textContent).toBe('· 14:02');
  });

  it('renders NO time span when ts is absent/null', () => {
    const noTs = { ...withTs({ uuid: 'h2', kind: 'human' }), ts: null } as unknown as ConversationItem;
    const { container } = render(<MessageItem item={noTs} />);
    expect(container.querySelector('.conv-item-time')).toBeNull();
  });

  it('honors the context fmtCtx tz — the same instant renders a different wall clock under a non-UTC zone', () => {
    // 14:02:31Z is 10:02 in America/New_York (EDT, -04 in June).
    fmtCtx.tz = 'America/New_York';
    fmtCtx.offsetLabel = 'EDT';
    const { container } = renderWithTz(withTs({ uuid: 'h3', kind: 'human' }));
    expect(container.querySelector('.conv-item-time')!.textContent).toBe('· 10:02');
  });
});

describe('MessageItem token footer (#177 S5 §6)', () => {
  const assistantBase: ConversationItem = {
    kind: 'assistant',
    anchor: { session_id: 's', uuid: 'a1', id: 1 },
    member_uuids: ['a1'],
    ts: '2026-06-12T14:02:31Z',
    text: 'hi',
    blocks: [{ kind: 'text', text: 'hi' }],
    model: 'claude-opus-4',
    is_sidechain: false,
    subagent_key: null,
    parent_uuid: null,
    cost_usd: 0.0214,
  };
  const tokens = { input: 1200, output: 4800, cache_creation: 10000, cache_read: 300000 };

  it('cost-only footer when tokens are absent (graceful degradation)', () => {
    const { container } = render(<MessageItem item={assistantBase} />);
    const cost = container.querySelector('.conv-item-cost')!;
    expect(cost.textContent).toBe('$0.0214');
    expect(cost.getAttribute('title')).toBeNull();
  });

  it('tokens-only footer when cost is zero but tokens are present', () => {
    const item = { ...assistantBase, cost_usd: 0, tokens } as ConversationItem;
    const { container } = render(<MessageItem item={item} />);
    const cost = container.querySelector('.conv-item-cost')!;
    expect(cost).not.toBeNull();
    // No leading "$..." and no leading " · " separator.
    expect(cost.textContent).toBe('in 1.2k · out 4.8k · cache 310k');
  });

  it('combined cost + tokens footer with the exact-count tooltip', () => {
    const item = { ...assistantBase, tokens } as ConversationItem;
    const { container } = render(<MessageItem item={item} />);
    const cost = container.querySelector('.conv-item-cost')!;
    expect(cost.textContent).toBe('$0.0214 · in 1.2k · out 4.8k · cache 310k');
    expect(cost.getAttribute('title')).toBe(
      'input 1200 · output 4800 · cache create 10000 · cache read 300000',
    );
  });

  it('omits the footer entirely when neither cost nor tokens are present', () => {
    const item = { ...assistantBase, cost_usd: 0 } as ConversationItem;
    delete (item as { tokens?: unknown }).tokens;
    const { container } = render(<MessageItem item={item} />);
    expect(container.querySelector('.conv-item-cost')).toBeNull();
  });

  // #191 — harness-injected user lines must never render as a "You" turn.
  it('renders a compaction meta row as "Compacted earlier conversation", never You', () => {
    const item: ConversationItem = {
      kind: 'meta',
      anchor: { session_id: 's', uuid: 'c1', id: 1 },
      member_uuids: ['c1'], ts: '2026-06-01T00:00:00Z',
      text: 'This session is being continued from a previous conversation…',
      blocks: [{ kind: 'text', text: 'This session is being continued from a previous conversation…' }],
      is_sidechain: false, subagent_key: null, parent_uuid: null,
      meta_kind: 'compaction', skill_name: null,
    };
    const { container } = render(<MessageItem item={item} />);
    expect(container.querySelector('.conv-item--human')).toBeNull();
    expect(container.querySelector('details.conv-meta--compaction')).not.toBeNull();
    expect(container.textContent).toContain('Compacted earlier conversation');
  });

  it('renders a notification meta row as "Background task" with the summary', () => {
    const body = '<task-notification>\n<summary>Run tests completed (exit code 0)</summary>\n</task-notification>';
    const item: ConversationItem = {
      kind: 'meta',
      anchor: { session_id: 's', uuid: 'n1', id: 1 },
      member_uuids: ['n1'], ts: '2026-06-01T00:00:00Z',
      text: body, blocks: [{ kind: 'text', text: body }],
      is_sidechain: false, subagent_key: null, parent_uuid: null,
      meta_kind: 'notification', skill_name: null,
    };
    const { container } = render(<MessageItem item={item} />);
    expect(container.querySelector('.conv-item--human')).toBeNull();
    expect(container.querySelector('details.conv-meta--notification')).not.toBeNull();
    expect(container.textContent).toContain('Background task');
    expect(container.textContent).toContain('Run tests completed (exit code 0)');
  });
});

// ---- cache-failure-markers spec §3 — the reader header chip --------------
describe('MessageItem cache-failure chip (cache-failure-markers spec §3)', () => {
  const flagged: ConversationItem = {
    kind: 'assistant',
    anchor: { session_id: 's', uuid: 'cf1', id: 1 },
    member_uuids: ['cf1'],
    ts: '2026-06-12T14:02:31Z',
    text: 'rebuilt the prefix',
    blocks: [{ kind: 'text', text: 'rebuilt the prefix' }],
    model: 'claude-opus-4-8',
    is_sidechain: false,
    subagent_key: null,
    parent_uuid: null,
    cost_usd: 1.0,
    // 130000 → fmt.compact upper "130K"; 0.7475 → fmt.usd2 "$0.75".
    cache_failure: { tokens_recreated: 130000, prev_cached: 130000, est_wasted_usd: 0.7475 },
  };

  it('renders the amber chip with text + aria-label when flagged AND markers on', () => {
    const { container } = renderWithMarkers(flagged, true);
    const chip = container.querySelector('.conv-item-head .conv-cache-chip')!;
    expect(chip).not.toBeNull();
    // The chip text carries the meaning (the ⚡ glyph is aria-hidden).
    expect(chip.textContent).toContain('CACHE REBUILT');
    expect(chip.textContent).toContain('130K');
    expect(chip.textContent).toContain('+$0.75');
    // The glyph is aria-hidden so screen readers read the chip text, not "⚡".
    expect(chip.querySelector('[aria-hidden="true"]')).not.toBeNull();
    // The accessible label spells the full explanation.
    const label = chip.getAttribute('aria-label') ?? '';
    expect(label.toLowerCase()).toContain('cache');
    expect(label).toContain('130,000'); // toLocaleString of tokens_recreated
  });

  it('renders the chip on a bare render (markers default ON)', () => {
    // No provider override → the context default markersEnabled is true.
    const { container } = render(<MessageItem item={flagged} />);
    expect(container.querySelector('.conv-cache-chip')).not.toBeNull();
  });

  it('HIDES the chip when markers are toggled off', () => {
    const { container } = renderWithMarkers(flagged, false);
    expect(container.querySelector('.conv-cache-chip')).toBeNull();
    // The turn still renders normally (just no marker).
    expect(container.querySelector('.conv-item--assistant')).not.toBeNull();
  });

  it('renders NO chip on a healthy turn (no cache_failure)', () => {
    const healthy: ConversationItem = { ...flagged, anchor: { session_id: 's', uuid: 'ok1', id: 2 } };
    delete (healthy as { cache_failure?: unknown }).cache_failure;
    const { container } = renderWithMarkers(healthy, true);
    expect(container.querySelector('.conv-cache-chip')).toBeNull();
  });
});

// #217 S6 F4 — the per-turn bookmark control rides in each .conv-item-actions
// row (an assistant turn with prose renders one).
describe('MessageItem bookmark control (#217 S6 F4)', () => {
  it('renders a .conv-bookmark-btn in the actions row of a prose assistant turn', () => {
    const { container } = renderWithTz(assistant);
    const actions = container.querySelector('.conv-item-actions')!;
    expect(actions.querySelector('.conv-bookmark-btn')).not.toBeNull();
  });
});
