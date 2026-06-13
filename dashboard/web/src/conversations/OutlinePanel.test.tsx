import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
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

describe('OutlinePanel (#186 §4 header redesign)', () => {
  it('renders a <nav aria-label="Session outline"> wrapper', () => {
    render(<OutlinePanel sessionId="s1" outline={outline()} />);
    expect(screen.getByRole('navigation', { name: 'Session outline' })).toBeTruthy();
  });

  it('renders the quiet placeholder when outline is null', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={null} />);
    expect(container.querySelector('.conv-outline-placeholder')).toBeTruthy();
    expect(container.querySelector('.conv-outline-list')).toBeNull();
  });

  it('headline shows total turns + yours', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const card = container.querySelector('.conv-outline-stats')!;
    expect(card.textContent).toContain('47');
    expect(card.textContent).toContain('9');
    expect(card.textContent).toContain('turns');
    expect(card.textContent).toContain('yours');
  });

  it('renders three stat tiles (Time / Tokens / Cost) with values + uppercase labels', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const tiles = container.querySelectorAll('.conv-outline-stat-tile');
    expect(tiles.length).toBe(3);
    const text = container.querySelector('.conv-outline-stat-tiles')!.textContent ?? '';
    // Time → fmt.hhmm → "3h 25m"; Cost → fmt.usd2 → "$4.20"; Tokens 7000 → "7k".
    expect(text).toContain('3h 25m');
    expect(text).toContain('7k');
    expect(text).toContain('$4.20');
    // Labels (case-insensitive — CSS uppercases; the DOM text is the source spelling).
    expect(text.toLowerCase()).toContain('time');
    expect(text.toLowerCase()).toContain('tokens');
    expect(text.toLowerCase()).toContain('cost');
  });

  it('renders Models / Tools labeled distribution rows', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const kvs = container.querySelectorAll('.conv-outline-stat-kv');
    const text = Array.from(kvs).map((k) => k.textContent).join('|');
    expect(text.toLowerCase()).toContain('models');
    expect(text.toLowerCase()).toContain('tools');
    expect(text).toContain('claude-opus-4');
    // Tools: top-3 by count + "+N more"; full list in title.
    const toolsRow = Array.from(kvs).find((k) => /tools/i.test(k.textContent ?? ''))!;
    expect(toolsRow.textContent).toContain('Read ×18');
    expect(toolsRow.textContent).toContain('+2 more');
    expect(toolsRow.textContent).not.toContain('Grep');
    expect(toolsRow.getAttribute('title')).toContain('Grep ×4');
  });

  it('hides the Errors row when error_count is 0', () => {
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={outline({ stats: stats({ error_count: 0 }) })} />,
    );
    const kvs = Array.from(container.querySelectorAll('.conv-outline-stat-kv'));
    expect(kvs.some((k) => /error/i.test(k.textContent ?? ''))).toBe(false);
  });

  // #186 §4.3 — reconcile the two error numbers. error_count (server total, 14)
  // appears with " in {errorTurns} turns" ONLY when the error-turn count (13)
  // differs; a clean 1:1 session just says "5 errors".
  it('reconciles "14 errors in 13 turns" when error_count exceeds the error-turn count', () => {
    // 13 distinct turns carrying an error; server error_count = 14.
    const errTurns: OutlineTurn[] = [];
    for (let i = 0; i < 13; i++) {
      errTurns.push(turn({ uuid: `e${i}`, kind: 'human', label: `p${i}` }));
      errTurns.push(turn({ uuid: `a${i}`, kind: 'assistant', label: 'oops', tools: [{ name: 'Bash', is_error: true }] }));
    }
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={outline({ stats: stats({ error_count: 14 }), turns: errTurns })} />,
    );
    const errRow = Array.from(container.querySelectorAll('.conv-outline-stat-kv'))
      .find((k) => /error/i.test(k.textContent ?? ''))!;
    const value = errRow.querySelector('.conv-outline-stat-kv-value')!.textContent ?? '';
    expect(value).toMatch(/14 errors in 13 turns/);
  });

  it('shows just "5 errors" (no "in N turns") when error_count equals the error-turn count', () => {
    const errTurns: OutlineTurn[] = [];
    for (let i = 0; i < 5; i++) {
      errTurns.push(turn({ uuid: `a${i}`, kind: 'assistant', label: 'oops', tools: [{ name: 'Bash', is_error: true }] }));
    }
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={outline({ stats: stats({ error_count: 5 }), turns: errTurns })} />,
    );
    const errRow = Array.from(container.querySelectorAll('.conv-outline-stat-kv'))
      .find((k) => /error/i.test(k.textContent ?? ''))!;
    // Assert against the VALUE span (not the row's concatenated textContent,
    // where the "Errors" label runs straight into "5 errors").
    const value = errRow.querySelector('.conv-outline-stat-kv-value')!.textContent ?? '';
    expect(value).toMatch(/^5 errors$/);
    expect(value).not.toMatch(/in .* turns/);
  });

  it('renders one list entry per derived landmark', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const entries = container.querySelectorAll('.conv-outline-entry');
    // h1, h2 are prompts; a1 is generic prose → dropped. Two entries.
    expect(entries.length).toBe(2);
    expect(entries[0].textContent).toContain('fix the bug');
    expect(entries[1].textContent).toContain('looks good');
  });

  it('renders a 🧠 ×N badge on a prompt row whose section has thinking', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'think' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'r', thinking: ['t1', 't2'] }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const badge = container.querySelector('.conv-outline-entry-thinking')!;
    expect(badge).toBeTruthy();
    expect(badge.textContent).toContain('2');
  });

  // #186 §3 scroll-sync — the cursor lands on a generic (non-landmark) turn's
  // member uuid; the section prompt entry gets aria-current via sectionByUuid.
  // Modal-level integration test (drives the panel, not a child unit).
  it('aria-current lands on the section prompt when the cursor is a non-landmark member', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'prompt' }),
        // generic assistant — produces NO entry — with a folded member 'a1b'.
        turn({ uuid: 'a1', kind: 'assistant', label: 'generic', member_uuids: ['a1', 'a1b'] }),
      ],
    });
    // Cursor on the folded member of the generic assistant turn.
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'a1b' });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current.length).toBe(1);
    expect(current[0].textContent).toContain('prompt'); // the section prompt
  });

  it('aria-current also lands on an exact landmark entry uuid match', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'prompt' }),
        turn({ uuid: 'a1', kind: 'assistant', label: '## A heading' }),
      ],
    });
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'a1' });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    // h1 (section of a1) AND a1 (exact landmark). Both legitimately current.
    const texts = Array.from(current).map((c) => c.textContent);
    expect(texts.some((t) => t?.includes('A heading'))).toBe(true);
  });

  it('clicking an entry dispatches OPEN_CONVERSATION with the jump anchor', () => {
    render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const btn = screen.getByRole('button', { name: /looks good/ });
    fireEvent.click(btn);
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'h2' });
    expect(getState().selectedConversationId).toBe('s1');
  });

  it('entry click in a focus mode that HIDES the target resets to all before jumping', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: '## heading section' }),
      ],
    });
    dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' });
    render(<OutlinePanel sessionId="s1" outline={o} />);
    // The heading landmark (assistant turn) is hidden in Prompts mode → reset.
    const btn = screen.getByRole('button', { name: /heading section/ });
    fireEvent.click(btn);
    expect(getState().convFocusMode).toBe('all');
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'a1' });
  });

  it('entry click on a target VISIBLE in the current mode does NOT reset the mode', () => {
    dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' });
    render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const btn = screen.getByRole('button', { name: /looks good/ });
    fireEvent.click(btn);
    expect(getState().convFocusMode).toBe('prompts');
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'h2' });
  });

  // #186 §4.1 — the jump cluster is MERGED INTO the stats card (no longer a
  // sibling above it) with visible text labels; the error chip reads
  // "error turns"; data-jump-kind attributes are preserved.
  it('renders the jump cluster INSIDE the stats card with labeled chips', () => {
    const o = outline({
      stats: stats({ error_count: 1 }),
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'oops', tools: [{ name: 'Bash', is_error: true }] }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const card = container.querySelector('.conv-outline-stats')!;
    const cluster = card.querySelector('.conv-jump-cluster')!;
    expect(cluster).toBeTruthy(); // inside the card, not a sibling
    // a "Jump to" label precedes the chips.
    expect(card.textContent).toMatch(/jump to/i);
    // chips present with data-jump-kind preserved.
    const promptChip = cluster.querySelector('[data-jump-kind="prompt"]')!;
    const errChip = cluster.querySelector('[data-jump-kind="error"]')!;
    expect(promptChip).toBeTruthy();
    expect(errChip).toBeTruthy();
    // visible text labels.
    expect(promptChip.textContent?.toLowerCase()).toContain('prompts');
    expect(errChip.textContent?.toLowerCase()).toContain('error turns');
  });

  it('clicking a cluster chip jumps to the next target; shift-click goes previous', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'one' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'work' }),
        turn({ uuid: 'h2', kind: 'human', label: 'two' }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const promptBtn = container.querySelector<HTMLButtonElement>('[data-jump-kind="prompt"]')!;
    fireEvent.click(promptBtn);
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'h1' });
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h2' });
    fireEvent.click(promptBtn, { shiftKey: true });
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'h1' });
  });

  it('the cluster is absent when no jump targets exist', () => {
    const o = outline({
      turns: [turn({ uuid: 'a1', kind: 'assistant', label: 'plain' })],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    expect(container.querySelector('.conv-jump-cluster')).toBeNull();
  });

  it('drops the per-entry "· N tools" suffix (noise lives in the stats histogram)', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: '## heading', tools: [{ name: 'Read', is_error: false }, { name: 'Bash', is_error: false }] }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    expect(container.querySelector('.conv-outline-entry-tools')).toBeNull();
    expect(container.textContent).not.toMatch(/· 2 tools/);
  });
});
