import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
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
    cache_saved_usd: 0,
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

  // #192 — when the scroll-sync cursor lands on a turn that IS ITSELF an outline
  // entry (a landmark — heading / plan / subagent), aria-current marks ONLY that
  // exact entry; the section-prompt fallback no longer ALSO lights the spine
  // prompt. Previously both were marked (the user-reported double-mark): a single
  // current item is the correct aria semantics and the intended behavior.
  it('a landmark cursor marks ONLY the exact landmark entry (no section-prompt double-mark)', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'prompt' }),
        turn({ uuid: 'a1', kind: 'assistant', label: '## A heading' }),
      ],
    });
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'a1' });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current.length).toBe(1);
    expect(current[0].textContent).toContain('A heading'); // exactly a1, not the h1 prompt
  });

  // #192 — the headline bug: a subagent is the LAST outline element. After a
  // click + free scroll the pin clears and the subagent card stays the
  // topmost-visible turn, reporting its bucket-root uuid to scroll-sync. The
  // subagent entry's uuid IS that bucket-root, so the exact match lights it —
  // but the section-prompt fallback must NOT also light the trailing "You"
  // prompt of its section. Exactly one aria-current, on the subagent.
  it('a trailing subagent cursor marks ONLY the subagent, not the section prompt (free scroll, no pin)', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'last prompt' }),
        // a subagent bucket whose root member is 'sa1' (the card's data-uuid).
        turn({ uuid: 'sa1', kind: 'human', label: 'task', subagent_key: 'k1', is_sidechain: true }),
        turn({ uuid: 'sa2', kind: 'assistant', label: 'work', subagent_key: 'k1', is_sidechain: true }),
      ],
    });
    // Free scroll, no pin: the subagent card is the topmost-visible element and
    // reports its bucket-root uuid 'sa1'.
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'sa1' });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current.length).toBe(1);
    expect(current[0].classList.contains('conv-outline-entry--subagent')).toBe(true);
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

  // #217 S3 E8 (chip primary-click — folded in from I-1's deferral, spec §3
  // surface 2): the chip PRIMARY click now jumps to the MOST-RECENT occurrence
  // (targets.<kind>.at(-1)) — a direct action, not stepping. SHIFT-click keeps
  // the existing previous-stepping. (The reader's u/U,e/E keys keep stepping;
  // a/L keys are the keyboard twins of this jump-to-last.)
  it('chip PRIMARY click jumps to the most-recent occurrence; shift-click steps previous', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'one' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'work' }),
        turn({ uuid: 'h2', kind: 'human', label: 'two' }),
        turn({ uuid: 'h3', kind: 'human', label: 'three' }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const promptBtn = container.querySelector<HTMLButtonElement>('[data-jump-kind="prompt"]')!;
    // Primary click → the LAST prompt (h3), regardless of cursor position.
    fireEvent.click(promptBtn);
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'h3' });
    // Shift-click → previous STEP from the cursor (now h3) → h2. act() so the
    // cursor prop flushes into JumpCluster before the click reads it.
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h3' }); });
    fireEvent.click(promptBtn, { shiftKey: true });
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'h2' });
  });

  it('the cluster is absent when no jump targets exist', () => {
    const o = outline({
      turns: [turn({ uuid: 'a1', kind: 'assistant', label: 'plain' })],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    expect(container.querySelector('.conv-jump-cluster')).toBeNull();
  });

  // #188 S2 — the explicit-selection pin takes precedence over the scroll-sync
  // cursor for aria-current. Pinning X marks exactly the X entry, NOT the X-1
  // section prompt the topmost-visible cursor would otherwise highlight (Bug 2).
  it('aria-current prefers the pin: pinning a landmark marks exactly that entry (no section fallback)', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'prompt' }),
        turn({ uuid: 'a1', kind: 'assistant', label: '## A heading' }),
      ],
    });
    // The scroll-sync cursor sits on the section prompt (above the centered
    // target) — today this would light h1. The pin overrides it to a1 exactly.
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' });
    dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'a1' });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current.length).toBe(1);
    expect(current[0].textContent).toContain('A heading'); // exactly a1, not h1
  });

  it('aria-current with a pin does NOT also light the X-1 section prompt', () => {
    // Two prompts; pin the SECOND. The first must not be aria-current even
    // though the scroll cursor (topmost-visible) points at it.
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' });
    dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'h2' });
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current.length).toBe(1);
    expect(current[0].textContent).toContain('looks good'); // h2, not h1
  });

  // #188 Bug 3 — a subagent entry's uuid is its bucket-root uuid; pinning that
  // root lights exactly the subagent entry (not the most-recent prompt).
  it('pinning a subagent bucket-root uuid lights the subagent entry', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        // a subagent bucket whose root member is 'sa1'.
        turn({ uuid: 'sa1', kind: 'human', label: 'task', subagent_key: 'k1', is_sidechain: true }),
        turn({ uuid: 'sa2', kind: 'assistant', label: 'work', subagent_key: 'k1', is_sidechain: true }),
      ],
    });
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' });
    dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'sa1' });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current.length).toBe(1);
    expect(current[0].classList.contains('conv-outline-entry--subagent')).toBe(true);
  });

  it('without a pin, aria-current keeps the legacy exact-OR-section behavior', () => {
    // Pin null → the section-prompt fallback still applies (regression guard).
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'prompt' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'generic', member_uuids: ['a1', 'a1b'] }),
      ],
    });
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'a1b' });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current.length).toBe(1);
    expect(current[0].textContent).toContain('prompt'); // section prompt fallback
  });

  // #188 / #187 — the headline regression. Two consecutive FORWARD jump-to-next
  // (prompt) clicks must land on two DISTINCT landmarks. Before the fix the
  // cursor read `convCurrentTurnUuid` (the scroll-sync topmost-visible turn,
  // which sits ABOVE the centered target), so the second forward click re-found
  // the SAME prompt. With the pin driving the cursor, the second click reads the
  // cursor at the landed index and steps strictly forward.
  // #188 S2 (#187) — the SHIFT-click stepping path still prefers the pin over the
  // lagging scroll cursor. (Primary click is now jump-to-last — #217 S3 E8 —, so
  // the #187 step-disambiguation concern now lives on the shift-click step.)
  it('shift-click prompt steps from the PIN, not the lagging scroll cursor (closes #187)', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'one' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'work' }),
        turn({ uuid: 'h2', kind: 'human', label: 'two' }),
        turn({ uuid: 'a2', kind: 'assistant', label: 'more work' }),
        turn({ uuid: 'h3', kind: 'human', label: 'three' }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const promptBtn = container.querySelector<HTMLButtonElement>('[data-jump-kind="prompt"]')!;

    // A previous jump LANDED on h2 (pin), but the scroll-sync observer reports
    // the topmost-VISIBLE turn h3 (BELOW the centered target). The pin records
    // the real landing (h2); the scroll cursor lags ahead at h3.
    act(() => {
      dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'h2' });
      dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h3' });
    });

    // Backward step: the pin (h2) wins over the scroll cursor (h3), so the cursor
    // resolves to h2's index and steps strictly backward to h1 — NOT to h2.
    fireEvent.click(promptBtn, { shiftKey: true });
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'h1' });
  });

  // ---- cache-failure-markers spec §4 — stats row + jump chip + opt-out ----
  const cf = { tokens_recreated: 130000, prev_cached: 130000, est_wasted_usd: 0.75 };

  it('renders a "Cache" stats KV row only when cache_failures.count > 0', () => {
    const { container } = render(
      <OutlinePanel
        sessionId="s1"
        outline={outline({ stats: stats({ cache_failures: { count: 2, tokens_recreated: 205000, est_wasted_usd: 1.18, rebuilds: [] } }) })}
      />,
    );
    const cacheRow = Array.from(container.querySelectorAll('.conv-outline-stat-kv'))
      .find((k) => /cache/i.test(k.textContent ?? ''));
    expect(cacheRow).toBeTruthy();
    expect(cacheRow!.textContent).toContain('2'); // 2 rebuilds
    expect(cacheRow!.classList.contains('conv-outline-stat-kv--cache')).toBe(true);
  });

  it('hides the "Cache" stats row when cache_failures is absent', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    const cacheRow = Array.from(container.querySelectorAll('.conv-outline-stat-kv'))
      .find((k) => /\bcache\b/i.test(k.textContent ?? ''));
    expect(cacheRow).toBeFalsy();
  });

  it('renders the ⚡ cache jump chip when flagged turns exist', () => {
    const o = outline({
      stats: stats({ cache_failures: { count: 1, tokens_recreated: 130000, est_wasted_usd: 0.75, rebuilds: [] } }),
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'rebuilt', cache_failure: cf }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const cacheChip = container.querySelector('[data-jump-kind="cache"]');
    expect(cacheChip).toBeTruthy();
    expect(cacheChip!.textContent?.toLowerCase()).toContain('cache');
  });

  it('clicking the cache jump chip jumps to the flagged turn', () => {
    const o = outline({
      stats: stats({ cache_failures: { count: 1, tokens_recreated: 130000, est_wasted_usd: 0.75, rebuilds: [] } }),
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'rebuilt', cache_failure: cf }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const cacheChip = container.querySelector<HTMLButtonElement>('[data-jump-kind="cache"]')!;
    fireEvent.click(cacheChip);
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'a1' });
  });

  it('renders a standalone cache landmark entry with the amber suffix glyph', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'rebuilt', cache_failure: cf }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const cacheEntry = container.querySelector('.conv-outline-entry--cache');
    expect(cacheEntry).toBeTruthy();
    expect(cacheEntry!.textContent?.toLowerCase()).toContain('cache rebuilt');
  });

  // ---- #217 S6 F3 — outline per-rebuild jump list ----------------------
  it('renders a per-rebuild jump list under the cache stat (markers on, count>0)', () => {
    const rebuilds = [
      { uuid: 'a1', subagent_key: null, ts: '2026-06-22T01:00:00Z', tokens_recreated: 120000, est_wasted_usd: 0.90 },
      { uuid: 'h2', subagent_key: null, ts: '2026-06-22T02:00:00Z', tokens_recreated: 80000, est_wasted_usd: 0.28 },
    ];
    const o = outline({ stats: stats({ cache_failures: { count: 2, tokens_recreated: 200000, est_wasted_usd: 1.18, rebuilds } }) });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const rows = container.querySelectorAll('.conv-outline-rebuilds .conv-rebuild-jump');
    expect(rows.length).toBe(2);
    // label resolves from outline.turns (a1 → "here is the plan")
    expect(container.querySelector('.conv-outline-rebuilds')!.textContent).toContain('here is the plan');
    // clicking dispatches a jump to the rebuild uuid
    fireEvent.click(rows[0]);
    expect(getState().conversationJump?.uuid).toBe('a1');
  });

  it('falls back to "turn" label when a rebuild uuid is absent from outline.turns', () => {
    const rebuilds = [
      { uuid: 'ghost', subagent_key: null, ts: '2026-06-22T01:00:00Z', tokens_recreated: 1, est_wasted_usd: 0.10 },
    ];
    const o = outline({ stats: stats({ cache_failures: { count: 1, tokens_recreated: 1, est_wasted_usd: 0.10, rebuilds } }) });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const row = container.querySelector('.conv-outline-rebuilds .conv-rebuild-jump')!;
    // No matching turn → the label falls back rather than rendering empty.
    expect(row.querySelector('.rb-label')!.textContent).toBe('turn');
  });

  it('caps the rebuild list at 3 with a "+N more" expander', () => {
    const rebuilds = Array.from({ length: 5 }, (_, i) => ({
      uuid: `r${i}`, subagent_key: null, ts: '2026-06-22T01:00:00Z',
      tokens_recreated: 1000 * (5 - i), est_wasted_usd: 0.5 - i * 0.05,
    }));
    const o = outline({ stats: stats({ cache_failures: { count: 5, tokens_recreated: 1, est_wasted_usd: 1, rebuilds } }) });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    expect(container.querySelectorAll('.conv-outline-rebuilds .conv-rebuild-jump').length).toBe(3);
    const more = container.querySelector('.conv-rebuild-more')!;
    expect(more.textContent).toContain('+2 more');
    fireEvent.click(more);
    expect(container.querySelectorAll('.conv-outline-rebuilds .conv-rebuild-jump').length).toBe(5);
  });

  it('hides the rebuild list when markers are off OR count is 0', () => {
    // count 0 → no list even with markers on.
    const noneOutline = outline({ stats: stats({ cache_failures: { count: 0, tokens_recreated: 0, est_wasted_usd: 0, rebuilds: [] } }) });
    const { container: c1 } = render(<OutlinePanel sessionId="s1" outline={noneOutline} />);
    expect(c1.querySelector('.conv-outline-rebuilds')).toBeNull();

    // markers off → no list even with count>0.
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: { cache_failure_markers: false } });
    const rebuilds = [{ uuid: 'a1', subagent_key: null, ts: null, tokens_recreated: 1, est_wasted_usd: 0.1 }];
    const offOutline = outline({ stats: stats({ cache_failures: { count: 1, tokens_recreated: 1, est_wasted_usd: 0.1, rebuilds } }) });
    const { container: c2 } = render(<OutlinePanel sessionId="s1" outline={offOutline} />);
    expect(c2.querySelector('.conv-outline-rebuilds')).toBeNull();
  });

  it('toggle OFF (dashboard_prefs) hides the cache stats row, jump chip, and landmark', () => {
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: { cache_failure_markers: false } });
    const o = outline({
      stats: stats({ cache_failures: { count: 1, tokens_recreated: 130000, est_wasted_usd: 0.75, rebuilds: [] } }),
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'rebuilt', cache_failure: cf }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    // No cache stats row.
    expect(
      Array.from(container.querySelectorAll('.conv-outline-stat-kv')).some((k) =>
        /\bcache\b/i.test(k.textContent ?? ''),
      ),
    ).toBe(false);
    // No cache jump chip.
    expect(container.querySelector('[data-jump-kind="cache"]')).toBeNull();
    // No standalone cache landmark.
    expect(container.querySelector('.conv-outline-entry--cache')).toBeNull();
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

  // ---- #217 S3 F8 — compaction landmark chip + jump --------------------
  it('renders a compaction jump chip (data-jump-kind="compaction") when a compaction turn exists', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'long' }),
        turn({ uuid: 'cx', kind: 'meta', label: 'compacted', meta_kind: 'compaction' }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const chip = container.querySelector('[data-jump-kind="compaction"]');
    expect(chip).toBeTruthy();
    expect(chip!.textContent?.toLowerCase()).toContain('compaction');
  });

  it('clicking the compaction chip jumps to the compaction turn', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'long' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'work' }),
        turn({ uuid: 'cx', kind: 'meta', label: 'compacted', meta_kind: 'compaction' }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const chip = container.querySelector<HTMLButtonElement>('[data-jump-kind="compaction"]')!;
    fireEvent.click(chip);
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'cx' });
  });

  it('renders a compaction OUTLINE entry (navigable) for a compaction turn', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'long' }),
        turn({ uuid: 'cx', kind: 'meta', label: 'Conversation compacted', meta_kind: 'compaction' }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const entry = container.querySelector('.conv-outline-entry--compaction');
    expect(entry).toBeTruthy();
    expect(entry!.textContent).toContain('Conversation compacted');
  });

  it('no compaction chip when there is no compaction turn', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    expect(container.querySelector('[data-jump-kind="compaction"]')).toBeNull();
  });

  // ---- #217 S3 E6(a) — per-subagent cost render -------------------------
  it('renders subagent cost from outline.subagent_costs on the subagent entry', () => {
    const o = outline({
      subagent_meta: { sk1: { kind: 'explore' } },
      subagent_costs: { sk1: 0.4231 },
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'dispatch' }),
        turn({ uuid: 's1', kind: 'assistant', label: 'sub', subagent_key: 'sk1', parent_uuid: 'x', is_sidechain: true }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const sub = container.querySelector('.conv-outline-entry--subagent')!;
    expect(sub).toBeTruthy();
    // fmt.usd2(0.4231) → "$0.42".
    expect(sub.textContent).toContain('$0.42');
  });

  it('renders cost for a subagent bucket whose subagent_meta is EMPTY (the s7 case)', () => {
    const o = outline({
      subagent_meta: {},               // no meta for the bucket
      subagent_costs: { ghostkey: 0.8 },
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'dispatch' }),
        turn({ uuid: 's1', kind: 'assistant', label: 'sub', subagent_key: 'ghostkey', parent_uuid: 'x', is_sidechain: true }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const sub = container.querySelector('.conv-outline-entry--subagent')!;
    expect(sub).toBeTruthy();
    expect(sub.textContent).toContain('$0.80');
  });

  it('renders no cost affordance when subagent_costs lacks the bucket', () => {
    const o = outline({
      subagent_meta: { sk1: { kind: 'explore' } },
      subagent_costs: {},              // no cost for sk1
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'dispatch' }),
        turn({ uuid: 's1', kind: 'assistant', label: 'sub', subagent_key: 'sk1', parent_uuid: 'x', is_sidechain: true }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const sub = container.querySelector('.conv-outline-entry--subagent')!;
    expect(sub.querySelector('.conv-outline-entry-cost')).toBeNull();
  });

  // ---- #217 S3 E6(c) — tree (nested subagents render indented) ----------
  it('a nested subagent renders indented (deeper depth class) beneath its parent bucket', () => {
    const o = outline({
      subagent_meta: {
        C: { kind: 'code-reviewer', parent_subagent_key: null },
        G: { kind: 'grounding', parent_subagent_key: 'C' },
      },
      subagent_costs: { C: 0.1, G: 0.05 },
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'audit' }),
        turn({ uuid: 'c1', kind: 'assistant', label: 'review', subagent_key: 'C', parent_uuid: null, is_sidechain: true }),
        turn({ uuid: 'g1', kind: 'assistant', label: 'ground', subagent_key: 'G', parent_uuid: null, is_sidechain: true }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const subs = Array.from(container.querySelectorAll('.conv-outline-entry--subagent'));
    expect(subs).toHaveLength(2);
    // The nested child (G) carries the depth-2 nesting modifier; its parent (C)
    // sits at depth 1 (the single-level --nested class).
    const cEntry = subs.find((e) => e.textContent?.includes('code-reviewer'))!;
    const gEntry = subs.find((e) => e.textContent?.includes('grounding'))!;
    expect(cEntry.className).toContain('conv-outline-entry--nested');
    // The grandchild indents deeper than its parent (a data attribute drives the
    // indent so we can assert the level without pixel math).
    const cDepth = Number(cEntry.getAttribute('data-depth'));
    const gDepth = Number(gEntry.getAttribute('data-depth'));
    expect(gDepth).toBe(cDepth + 1);
  });
});

// #217 S5 F2 — the [Outline] [Files] tab toggle inside the outline panel.
describe('OutlinePanel — Files tab (#217 S5 F2)', () => {
  const filesOutline = () =>
    outline({
      files: [
        {
          path: 'bin/resolve.py',
          add: 412,
          del: 87,
          touches: [{ uuid: 'a1', tool_use_id: 't1', op: 'edit', add: 412, del: 87 }],
        },
      ],
    });

  it('starts on the Outline tab; the list is shown, not the files panel', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={filesOutline()} />);
    expect(getState().convOutlineTab).toBe('outline');
    expect(container.querySelector('.conv-outline-list')).toBeTruthy();
    expect(container.querySelector('.conv-outline-files')).toBeNull();
  });

  it('shows the files count badge on the Files tab', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={filesOutline()} />);
    const filesTab = screen.getByRole('tab', { name: /files/i });
    expect(filesTab.querySelector('.conv-outline-tab-count')?.textContent).toBe('1');
    expect(container).toBeTruthy();
  });

  it('switches to the Files tab and renders the file rows', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={filesOutline()} />);
    fireEvent.click(screen.getByRole('tab', { name: /files/i }));
    expect(getState().convOutlineTab).toBe('files');
    expect(container.querySelector('.conv-outline-files')).toBeTruthy();
    expect(container.querySelector('.conv-outline-list')).toBeNull();
    expect(screen.getByText('resolve.py')).toBeInTheDocument();
  });

  it('a touch-row click jumps to the touch anchor via OPEN_CONVERSATION', () => {
    render(<OutlinePanel sessionId="s1" outline={filesOutline()} />);
    fireEvent.click(screen.getByRole('tab', { name: /files/i }));
    fireEvent.click(screen.getByRole('button', { name: /resolve\.py/i }));
    fireEvent.click(screen.getByRole('button', { name: /edit/i }));
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'a1' });
  });
});
