import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { OutlinePanel } from './OutlinePanel';
import { _resetForTests, dispatch, getState } from '../store/store';
import type {
  ConversationOutline,
  OutlineStats,
  OutlineTurn,
} from '../types/conversation';

// Minimal OutlineTurn factory (mirrors deriveOutline.test.ts).
function turn(
  over: Partial<OutlineTurn> & { uuid: string; kind: OutlineTurn['kind'] },
): OutlineTurn {
  return {
    ts: null,
    label: '',
    member_uuids: [over.uuid],
    subagent_key: null,
    parent_uuid: null,
    is_sidechain: false,
    ...over,
  };
}

function stats(over: Partial<OutlineStats> = {}): OutlineStats {
  return {
    turns: { total: 47, human: 9, assistant: 30, tool_result: 6, meta: 2 },
    tool_counts: { Read: 18, Bash: 12, Edit: 7, Grep: 4, Write: 2 },
    error_count: 0,
    models: { 'claude-opus-4': 30 },
    duration_seconds: 3 * 3600 + 25 * 60, // 3h 25m
    tokens: { input: 1200, output: 800, cache_creation: 0, cache_read: 5000 },
    cost_usd: 4.2,
    ...over,
  };
}

function outline(over: Partial<ConversationOutline> = {}): ConversationOutline {
  return {
    session_id: 's1',
    stats: stats(),
    turns: [
      turn({ uuid: 'h1', kind: 'human', label: 'fix the bug' }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'here is the plan' }),
      turn({ uuid: 'h2', kind: 'human', label: 'looks good' }),
    ],
    ...over,
  };
}

beforeEach(() => {
  _resetForTests();
  dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's1' });
});
afterEach(() => {
  _resetForTests();
  vi.restoreAllMocks();
});

describe('OutlinePanel (#177 S5 §3)', () => {
  it('renders a <nav aria-label="Session outline"> wrapper', () => {
    render(<OutlinePanel sessionId="s1" outline={outline()} />);
    expect(screen.getByRole('navigation', { name: 'Session outline' })).toBeTruthy();
  });

  it('renders the quiet placeholder when outline is null', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={null} />);
    expect(container.querySelector('.conv-outline-placeholder')).toBeTruthy();
    expect(container.querySelector('.conv-outline-list')).toBeNull();
  });

  it('stats card shows turns / duration / tokens / cost', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const card = container.querySelector('.conv-outline-stats')!;
    expect(card).toBeTruthy();
    expect(card.textContent).toContain('47'); // total turns
    expect(card.textContent).toContain('9'); // yours
    expect(card.textContent).toContain('turns');
    expect(card.textContent).toContain('yours');
    // duration via fmt.hhmm → "3h 25m"; cost via fmt.usd2 → "$4.20"
    expect(card.textContent).toContain('3h 25m');
    expect(card.textContent).toContain('$4.20');
    // tokens via fmt.tokens: 1200+800+0+5000 = 7000 → "7k"
    expect(card.textContent).toContain('7k');
  });

  it('hides the error row when error_count is 0, shows it otherwise', () => {
    const { container, rerender } = render(
      <OutlinePanel sessionId="s1" outline={outline({ stats: stats({ error_count: 0 }) })} />,
    );
    expect(container.querySelector('.conv-outline-stats-errors')).toBeNull();

    rerender(
      <OutlinePanel sessionId="s1" outline={outline({ stats: stats({ error_count: 3 }) })} />,
    );
    const errRow = container.querySelector('.conv-outline-stats-errors')!;
    expect(errRow).toBeTruthy();
    expect(errRow.textContent).toContain('3 errors');
  });

  it('tool histogram shows the top-3 by count + "+N more" with full list in a title tooltip', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const hist = container.querySelector('.conv-outline-stats-tools')!;
    expect(hist).toBeTruthy();
    // top-3 by count: Read(18), Bash(12), Edit(7); 2 more (Grep, Write).
    expect(hist.textContent).toContain('Read ×18');
    expect(hist.textContent).toContain('Bash ×12');
    expect(hist.textContent).toContain('Edit ×7');
    expect(hist.textContent).toContain('+2 more');
    expect(hist.textContent).not.toContain('Grep'); // demoted to the tooltip
    // full list (all 5) lives in the title tooltip.
    const title = hist.getAttribute('title')!;
    expect(title).toContain('Read ×18');
    expect(title).toContain('Grep ×4');
    expect(title).toContain('Write ×2');
  });

  it('renders one list entry per derived landmark with React-keyed identity', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const entries = container.querySelectorAll('.conv-outline-entry');
    expect(entries.length).toBe(3); // h1, a1, h2
    expect(entries[0].textContent).toContain('fix the bug');
    expect(entries[1].textContent).toContain('here is the plan');
  });

  it('marks aria-current on the top-level entry whose uuid === convCurrentTurnUuid (not its thinking children)', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'prompt' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'reply', thinking: ['pondering'] }),
      ],
    });
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'a1' });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    // exactly one: the depth-0 assistant entry, NOT the depth-1 thinking child
    // (which shares the same uuid).
    expect(current.length).toBe(1);
    expect(current[0].textContent).toContain('reply');
    expect(current[0].classList.contains('conv-outline-entry--nested')).toBe(false);
  });

  it('clicking an entry dispatches OPEN_CONVERSATION with the jump anchor', () => {
    render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const btn = screen.getByRole('button', { name: /here is the plan/ });
    fireEvent.click(btn);
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'a1' });
    expect(getState().selectedConversationId).toBe('s1');
  });

  it('clicking the error stats row jumps to the first error entry', () => {
    const o = outline({
      stats: stats({ error_count: 2 }),
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({
          uuid: 'a1',
          kind: 'assistant',
          label: 'oops',
          tools: [{ name: 'Bash', is_error: true }],
        }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const errRow = container.querySelector<HTMLButtonElement>('.conv-outline-stats-errors')!;
    fireEvent.click(errRow);
    // a1 is the first entry carrying an error → jump anchors on it.
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'a1' });
  });

  it('a · N tools annotation renders only when toolCount > 0', () => {
    const o = outline({
      turns: [
        turn({
          uuid: 'a1',
          kind: 'assistant',
          label: 'work',
          tools: [
            { name: 'Read', is_error: false },
            { name: 'Bash', is_error: false },
          ],
        }),
        turn({ uuid: 'h1', kind: 'human', label: 'thanks' }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const entries = container.querySelectorAll('.conv-outline-entry');
    const work = within(entries[0] as HTMLElement);
    expect(work.getByText(/2 tools/)).toBeTruthy();
    // the human turn has no tools annotation.
    expect((entries[1] as HTMLElement).querySelector('.conv-outline-entry-tools')).toBeNull();
  });
});
