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
    // No matching turn (no skeleton index) → bare "turn" fallback.
    expect(row.querySelector('.rb-label')!.textContent).toBe('turn');
  });

  it('uses the indexed "turn N" label when an in-skeleton rebuild turn has no prose label (#226)', () => {
    // A tool-only assistant turn has label '' server-side (_outline_label → '');
    // it IS in the skeleton, so the label falls back to its 1-based index, not "turn".
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'fix the bug' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'here is the plan' }),
        turn({ uuid: 'tool', kind: 'assistant', label: '' }), // index 2
      ],
      stats: stats({ cache_failures: { count: 1, tokens_recreated: 1, est_wasted_usd: 0.10, rebuilds: [
        { uuid: 'tool', subagent_key: null, ts: null, tokens_recreated: 1, est_wasted_usd: 0.10 },
      ] } }),
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const row = container.querySelector('.conv-outline-rebuilds .conv-rebuild-jump')!;
    expect(row.querySelector('.rb-label')!.textContent).toBe('turn 3'); // 1-based index 2 → "turn 3"
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

// #304 S3 (Q5) — outline coverage pins. Q5 is COVERAGE ONLY: no outline CSS/DOM
// changes this session. These lock in the single-line label contract (truncation
// is CSS ellipsis, never a DOM change) and the state landmarks/glyphs so the S3
// typography raises + guard can't silently regress the outline. Cross-negatives
// keep each pin non-vacuous (a cache glyph is NOT the compaction glyph).
describe('outline coverage pins — no visual change (#304 S3 Q5)', () => {
  // 148 chars — a genuinely long technical prompt label.
  const LONG = 'refactor the reader header into intent clusters and verify the density resolver measures the .conv-reader root element width across the outline squeeze';

  it('a long entry label stays single-line-structured: full text in one .conv-outline-entry-label span + the full title, no wrap markup', () => {
    expect(LONG.length).toBeGreaterThanOrEqual(120);
    const o = outline({ turns: [turn({ uuid: 'h1', kind: 'human', label: LONG })] });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const entry = container.querySelector('.conv-outline-entry--human')!;
    const labels = entry.querySelectorAll('.conv-outline-entry-label');
    expect(labels.length).toBe(1);                       // one span, not a wrapped multi-node structure
    expect(labels[0].textContent).toBe(LONG);            // full label in the DOM (CSS ellipsis, not DOM truncation)
    expect(labels[0].querySelector('*')).toBeNull();     // no nested wrap markup added
    expect(entry.getAttribute('title')).toBe(LONG);      // the full label is the hover title
  });

  it('the cache state landmark renders its amber ⚡ glyph (not the compaction/completion glyph)', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'rebuilt', cache_failure: { tokens_recreated: 130000, prev_cached: 130000, est_wasted_usd: 0.75 } }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const entry = container.querySelector('.conv-outline-entry--cache')!;
    expect(entry.querySelector('.conv-outline-entry-cache-glyph')).toBeTruthy();
    expect(entry.querySelector('.conv-outline-entry-compaction-glyph')).toBeNull();
    expect(entry.querySelector('.conv-outline-entry-completion-glyph')).toBeNull();
  });

  it('the compaction state landmark renders its ⊟ glyph', () => {
    const o = outline({
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'cx', kind: 'meta', label: 'Conversation compacted', meta_kind: 'compaction' }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const entry = container.querySelector('.conv-outline-entry--compaction')!;
    expect(entry.querySelector('.conv-outline-entry-compaction-glyph')).toBeTruthy();
    expect(entry.querySelector('.conv-outline-entry-cache-glyph')).toBeNull();
  });

  it('the completion state landmark renders its ✓ glyph when task_completion.all_done', () => {
    const o = outline({
      task_completion: { all_done: true, total: 5, completed: 5, anchor_uuid: 'a1' },
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'done' }),
      ],
    });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const entry = container.querySelector('.conv-outline-entry--completion')!;
    expect(entry).toBeTruthy();
    expect(entry.querySelector('.conv-outline-entry-completion-glyph')).toBeTruthy();
    expect(entry.querySelector('.conv-outline-entry-compaction-glyph')).toBeNull();
  });

  it('the outline tabs render the Files count badge', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline()} />);
    expect(container.querySelectorAll('.conv-outline-tab').length).toBeGreaterThan(0);
    const count = container.querySelector('.conv-outline-tab-count')!;
    expect(count).toBeTruthy();
    expect(count.textContent).toBe('0'); // empty files → the "0" affordance still renders
  });
});

// #463 S4 §6.5 — the ERRORS row is THREE-state, because `0` and `null` are
// different claims. A positive count renders; `0` hides the row, matching the
// Claude side, because hiding is not a claim; `null` renders an explicit
// declined state, because rendering `0` would assert an absence nobody proved —
// the literal defect F13 names.
describe('#463 S4 — the three-state ERRORS row', () => {
  const errorsRow = (container: HTMLElement) =>
    Array.from(container.querySelectorAll('.conv-outline-stat-kv'))
      .find((kv) => /error/i.test(kv.textContent ?? '')) ?? null;

  it('renders an explicit declined state for a null error count', () => {
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={outline({ stats: stats({ error_count: null }) })} />,
    );
    const row = errorsRow(container);
    expect(row).not.toBeNull();
    expect(row!.textContent).toMatch(/not reported/i);
    expect(row!.textContent).not.toMatch(/\b0 errors\b/);
  });

  it('still hides the row at a determinable zero', () => {
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={outline({ stats: stats({ error_count: 0 }) })} />,
    );
    expect(errorsRow(container)).toBeNull();
  });

  it('still renders a positive count', () => {
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={outline({ stats: stats({ error_count: 2 }) })} />,
    );
    expect(errorsRow(container)!.textContent).toMatch(/2 errors/);
  });
});

// #463 S4 §6.7 — the tier-2 rows in the rendered rail. JSDOM cannot evaluate the
// indent, the wrap or the severity colour; what it CAN pin is that the landmark
// reaches the rail at all, as a button, carrying its own class, and jumping to
// the SEGMENT anchor rather than to its turn. The real-browser gate covers the
// rest.
describe('#463 S4 — tier-2 landmark rows', () => {
  const withLandmarks = () => outline({
    stats: stats({ error_count: 1 }),
    turns: [
      turn({ uuid: 'h1', kind: 'human', label: 'fix the build' }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'working on it' }),
    ],
    landmarks: [
      {
        landmark_key: 'cbk1.e#tool_error', block_key: 'cbk1.e', uuid: 'seg-15',
        parent_uuid: 'a1', kind: 'tool_error', label: 'exec', ts: null,
      },
      {
        landmark_key: 'cbk1.r#0', block_key: 'cbk1.r', uuid: 'seg-2',
        parent_uuid: 'a1', kind: 'reasoning',
        label: 'Reading the failing case before touching anything', ts: null,
      },
    ],
  });

  it('renders each landmark as its own button, indented under its turn', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={withLandmarks()} />);
    const rows = Array.from(container.querySelectorAll('.conv-outline-entry--landmark'));
    expect(rows).toHaveLength(2);
    // The row-to-cell-button pattern: the row IS the button, rather than a
    // clickable row with interactive children.
    expect(rows.every((row) => row.tagName === 'BUTTON')).toBe(true);
    expect(rows.map((row) => row.getAttribute('data-depth'))).toEqual(['2', '2']);
    expect(rows[0].className).toContain('conv-outline-entry--error');
    expect(rows[0].textContent).toContain('tool error · exec');
    expect(rows[1].textContent).toContain('Reading the failing case');
  });

  it('jumps to the landmark segment rather than to its owning turn', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={withLandmarks()} />);
    const row = container.querySelector('.conv-outline-entry--landmark')!;
    act(() => { fireEvent.click(row); });
    // #463 S4 F-A — the segment is the item the jump LOADS; `inner_anchor_key`
    // is the block inside it the jump ALIGNS. This case renders under focus
    // mode `all`, so it cannot observe the focus-mode reset — the remediation
    // describe below covers that.
    expect(getState().conversationJump).toEqual({
      session_id: 's1', uuid: 'seg-15', inner_anchor_key: 'cbk1.e',
    });
  });
});


// #463 S4 remediation — the three defects the S4 gates found in the landmark
// chrome. Each case below is one the shipped code passed while being wrong, so
// each fixture is built to make the wrong answer visible rather than incidental.
describe('#463 S4 remediation — landmark counts, jumps and focus mode', () => {
  // ONE turn owning THREE failures, which is the shape the gate measured on the
  // real store: nine error landmarks, all nine parented to a single turn.
  const threeFailuresOneTurn = (over: Partial<ConversationOutline> = {}) => outline({
    stats: stats({ error_count: 3 }),
    turns: [
      turn({ uuid: 'h1', kind: 'human', label: 'run the suite' }),
      turn({
        uuid: 'a1', kind: 'assistant', label: 'running it',
        segment_uuids: ['a1', 'seg-2', 'seg-9'],
        tools: [{ name: 'exec', is_error: true }],
      }),
    ],
    landmarks: [
      {
        landmark_key: 'cbk1.e1#tool_error', block_key: 'cbk1.e1', uuid: 'a1',
        parent_uuid: 'a1', kind: 'tool_error', label: 'exec', ts: null,
      },
      {
        landmark_key: 'cbk1.e2#tool_error', block_key: 'cbk1.e2', uuid: 'seg-2',
        parent_uuid: 'a1', kind: 'tool_error', label: 'exec', ts: null,
      },
      {
        landmark_key: 'cbk1.e3#tool_error', block_key: 'cbk1.e3', uuid: 'seg-9',
        parent_uuid: 'a1', kind: 'tool_error', label: 'exec', ts: null,
      },
    ],
    ...over,
  });

  // ── F-A ───────────────────────────────────────────────────────────────────
  it('carries the failing block, not only its segment, on a rail-row jump', () => {
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={threeFailuresOneTurn()} />,
    );
    const rows = container.querySelectorAll('.conv-outline-entry--landmark');
    act(() => { fireEvent.click(rows[2]); });
    expect(getState().conversationJump)
      .toEqual({ session_id: 's1', uuid: 'seg-9', inner_anchor_key: 'cbk1.e3' });
  });

  it('carries the failing block on the error CHIP jump too', () => {
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={threeFailuresOneTurn()} />,
    );
    const chip = container.querySelector('[data-jump-kind="error"]')!;
    act(() => { fireEvent.click(chip); });
    expect(getState().conversationJump)
      .toEqual({ session_id: 's1', uuid: 'seg-9', inner_anchor_key: 'cbk1.e3' });
  });

  // ── F-B ───────────────────────────────────────────────────────────────────
  it('counts error TURNS on the chip, not failing calls', () => {
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={threeFailuresOneTurn()} />,
    );
    const chip = container.querySelector('[data-jump-kind="error"]')!;
    expect(chip.textContent).toContain('error turns');
    expect(chip.querySelector('.conv-jump-cluster-count')!.textContent).toBe('1');
    expect(chip.getAttribute('aria-label')).toBe('Latest error turn, 1 total');
  });

  it('leaves the plan chip counting plans, whose label does not say turns', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline({
      turns: [turn({ uuid: 'a1', kind: 'assistant', label: 'planning' })],
      landmarks: [
        {
          landmark_key: 'cbk1.p1#plan', block_key: 'cbk1.p1', uuid: 'a1',
          parent_uuid: 'a1', kind: 'plan', label: 'update_plan', ts: null,
        },
        {
          landmark_key: 'cbk1.p2#plan', block_key: 'cbk1.p2', uuid: 'a1',
          parent_uuid: 'a1', kind: 'plan', label: 'update_plan', ts: null,
        },
      ],
    })} />);
    const chip = container.querySelector('[data-jump-kind="plan"]')!;
    expect(chip.querySelector('.conv-jump-cluster-count')!.textContent).toBe('2');
  });

  it('reconciles "3 errors in 1 turn" on the stats card', () => {
    // The phrase pluralized `error` and not `turn`, so this assertion pinned
    // "in 1 turns" until the remediation round fixed the noun it missed.
    // Round 3 — with the negative guard the positive one needs: "in 1 turns"
    // CONTAINS "in 1 turn", so `toContain` alone passes against the defect this
    // test names.
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={threeFailuresOneTurn()} />,
    );
    const row = container.querySelector('.conv-outline-stat-kv--errors')!;
    expect(row.textContent).toContain('3 errors in 1 turn');
    expect(row.textContent).not.toContain('1 turns');
  });

  it('still says a bare count when every failure is its own turn', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline({
      stats: stats({ error_count: 1 }),
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'ok',
               tools: [{ name: 'Bash', is_error: true }] }),
      ],
    })} />);
    const row = container.querySelector('.conv-outline-stat-kv--errors')!;
    // Round 3 — "1 errors" contains "1 error", so the positive assertion alone
    // could not see a singular-noun defect. The QA sweep of 45 Claude and 60
    // Codex conversations found none with exactly one error turn, so nothing
    // rendered this branch in the browser either.
    expect(row.textContent).toContain('1 error');
    expect(row.textContent).not.toMatch(/1 errors/);
    expect(row.textContent).not.toContain('in 1 turns');
  });

  // The other singular: the count inside the RECONCILED phrase, where the noun
  // that must not gain an `s` is `error` rather than `turn`. The pairing is
  // deliberate rather than natural — the server counts error events and the
  // client counts landmark-owning turns, so one event over two turns is a real
  // disagreement the phrase exists to report, and it is the only shape that
  // reaches `plural(1, 'error')` with the "in N turns" tail attached.
  it('says "1 error in 2 turns" rather than "1 errors in 2 turns"', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline({
      stats: stats({ error_count: 1 }),
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'go' }),
        turn({ uuid: 'a1', kind: 'assistant', label: 'first',
               tools: [{ name: 'Bash', is_error: true }] }),
        turn({ uuid: 'a2', kind: 'assistant', label: 'second',
               tools: [{ name: 'Bash', is_error: true }] }),
      ],
    })} />);
    const row = container.querySelector('.conv-outline-stat-kv--errors')!;
    expect(row.textContent).toContain('1 error in 2 turns');
    expect(row.textContent).not.toMatch(/1 errors/);
  });

  // ── F-C ───────────────────────────────────────────────────────────────────
  it('resets a focus mode that would hide a tier-2 row it is jumping to', () => {
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'edits' }); });
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={threeFailuresOneTurn()} />,
    );
    const rows = container.querySelectorAll('.conv-outline-entry--landmark');
    act(() => { fireEvent.click(rows[1]); });
    // The owning turn carries an `exec` call and no edit, so `edits` hides it;
    // without the reset the jump would load a key mounted nowhere.
    expect(getState().convFocusMode).toBe('all');
    expect(getState().conversationJump)
      .toEqual({ session_id: 's1', uuid: 'seg-2', inner_anchor_key: 'cbk1.e2' });
  });

  it('resets the focus mode for a Codex file touch on a segment key', () => {
    act(() => {
      dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'edits' });
      dispatch({ type: 'SET_CONV_OUTLINE_TAB', tab: 'files' });
    });
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={threeFailuresOneTurn({
        files: [{
          path: 'bin/x.py', add: 3, del: 1,
          touches: [{ uuid: 'seg-9', tool_use_id: null, op: 'update', add: null, del: null }],
        }],
      })} />,
    );
    act(() => { fireEvent.click(container.querySelector('.conv-files-file')!); });
    act(() => { fireEvent.click(container.querySelector('.conv-files-touch')!); });
    expect(getState().convFocusMode).toBe('all');
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'seg-9' });
  });

  it('leaves a focus mode that already shows the target turn alone', () => {
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'errors' }); });
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={threeFailuresOneTurn()} />,
    );
    const rows = container.querySelectorAll('.conv-outline-entry--landmark');
    act(() => { fireEvent.click(rows[0]); });
    expect(getState().convFocusMode).toBe('errors');
  });

  // ── F-G ───────────────────────────────────────────────────────────────────
  it('labels a plan landmark rather than repeating the bare word', () => {
    const { container } = render(<OutlinePanel sessionId="s1" outline={outline({
      turns: [turn({ uuid: 'a1', kind: 'assistant', label: 'planning' })],
      landmarks: [
        {
          landmark_key: 'cbk1.p1#plan', block_key: 'cbk1.p1', uuid: 'a1',
          parent_uuid: 'a1', kind: 'plan', label: 'update_plan', ts: null,
        },
        {
          landmark_key: 'cbk1.p2#plan', block_key: 'cbk1.p2', uuid: 'a1',
          parent_uuid: 'a1', kind: 'plan', label: 'update_plan', ts: null,
        },
      ],
    })} />);
    const rows = Array.from(container.querySelectorAll('.conv-outline-entry--landmark'));
    expect(rows.map((row) => row.textContent!.trim())).toEqual(['plan 1', 'plan 2']);
  });
});

// The remediation round the browser gate forced. Every unit gate on S4 passed
// while landmark stepping was dead in the browser, because each of the three
// entry points into the jump pipeline was tested only on its FIRST jump, which
// is the one jump that works from a cold cursor. These tests exercise the state
// the browser reached: a landmark jump has already landed and pinned a SEGMENT
// key, and the next press has to continue from there.
describe('#463 S4 remediation round 2 — the landmark navigation entry points', () => {
  // Two owning turns, each holding one failing call inside a follower segment.
  // Two owners is the minimum shape that can observe stepping at all: stepping
  // is turn-granular, so a single-owner fixture makes every target one stop and
  // a forward press has nowhere to go whether the cursor resolved or not.
  const twoOwners = (over: Partial<ConversationOutline> = {}) => outline({
    stats: stats({ error_count: 2 }),
    turns: [
      turn({ uuid: 'h1', kind: 'human', label: 'run the suite' }),
      turn({
        uuid: 'a1', kind: 'assistant', label: 'first attempt',
        segment_uuids: ['a1', 'seg-a'], tools: [{ name: 'exec', is_error: true }],
      }),
      turn({ uuid: 'h2', kind: 'human', label: 'try again' }),
      turn({
        uuid: 'a2', kind: 'assistant', label: 'second attempt',
        segment_uuids: ['a2', 'seg-b'], tools: [{ name: 'exec', is_error: true }],
      }),
    ],
    landmarks: [
      {
        landmark_key: 'cbk.e1#tool_error', block_key: 'cbk.e1', uuid: 'seg-a',
        parent_uuid: 'a1', kind: 'tool_error', label: 'exec', ts: null,
      },
      {
        landmark_key: 'cbk.e2#tool_error', block_key: 'cbk.e2', uuid: 'seg-b',
        parent_uuid: 'a2', kind: 'tool_error', label: 'exec', ts: null,
      },
    ],
    ...over,
  });

  // C-1 — the pin a landmark jump leaves behind is a SEGMENT key, and the chip
  // resolved its cursor through the own-uuid map alone, which does not hold
  // segment keys. The cursor therefore stayed at -1 and a backward step found
  // nothing at all, which is what the browser saw.
  it('steps backward from a pin left by a previous landmark jump', () => {
    act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'seg-b', anchorKey: 'cbk.e2' }); });
    const { container } = render(<OutlinePanel sessionId="s1" outline={twoOwners()} />);
    const chip = container.querySelector<HTMLElement>('[data-jump-kind="error"]')!;
    act(() => { fireEvent.click(chip, { shiftKey: true }); });
    expect(getState().conversationJump)
      .toEqual({ session_id: 's1', uuid: 'seg-a', inner_anchor_key: 'cbk.e1' });
  });

  // The same defect seen from the other side: with the cursor stuck at -1 a
  // backward step is not merely wrong, it reports "no such target" by pulsing.
  it('does not report an empty family when a landmark pin is set', () => {
    act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'seg-b', anchorKey: 'cbk.e2' }); });
    const { container } = render(<OutlinePanel sessionId="s1" outline={twoOwners()} />);
    const chip = container.querySelector<HTMLElement>('[data-jump-kind="error"]')!;
    act(() => { fireEvent.click(chip, { shiftKey: true }); });
    expect(chip.classList.contains('conv-pulse-disabled')).toBe(false);
  });

  // C-3 — the rail's pinned highlight followed only its own row click, so a
  // chip or keyboard jump left the highlight where the last CLICK put it. The
  // pin now carries the inner anchor the jump actually used, so the rail marks
  // the row that jump landed on whichever entry point issued it.
  const twoLandmarksOneSegment = () => outline({
    stats: stats({ error_count: 2 }),
    turns: [
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      turn({
        uuid: 'a1', kind: 'assistant', label: 'working',
        segment_uuids: ['a1', 'seg-2'], tools: [{ name: 'exec', is_error: true }],
      }),
    ],
    landmarks: [
      {
        landmark_key: 'cbk.e1#tool_error', block_key: 'cbk.e1', uuid: 'seg-2',
        parent_uuid: 'a1', kind: 'tool_error', label: 'first', ts: null,
      },
      {
        landmark_key: 'cbk.e2#tool_error', block_key: 'cbk.e2', uuid: 'seg-2',
        parent_uuid: 'a1', kind: 'tool_error', label: 'second', ts: null,
      },
    ],
  });

  it('marks the row a chip or keyboard jump landed on, not every row sharing its segment', () => {
    act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'seg-2', anchorKey: 'cbk.e2' }); });
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={twoLandmarksOneSegment()} />,
    );
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current).toHaveLength(1);
    expect(current[0].textContent).toContain('second');
  });

  it('still marks exactly the row the rail itself was clicked on', () => {
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={twoLandmarksOneSegment()} />,
    );
    const rows = container.querySelectorAll('.conv-outline-entry--landmark');
    act(() => { fireEvent.click(rows[0]); });
    // The reader owns the pin, so mirror what its landing bookkeeping writes.
    act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'seg-2', anchorKey: 'cbk.e1' }); });
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current).toHaveLength(1);
    expect(current[0].textContent).toContain('first');
  });

  // A pin with no inner anchor (a prompt jump, a deep link, a reading-position
  // restore) still marks the entry that owns the uuid.
  it('falls back to the uuid when the pin names no inner anchor', () => {
    act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'h2' }); });
    const { container } = render(<OutlinePanel sessionId="s1" outline={twoOwners()} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current).toHaveLength(1);
    expect(current[0].textContent).toContain('try again');
  });

  // #492 case 1 — a saved reading position carries only the segment key. It
  // restores the segment itself, not the finer failing call that an earlier
  // landmark jump used inside that segment. The rail must therefore mark the
  // containing prompt, not claim that the off-screen call is current.
  it('a bare segment restore marks its section prompt, not an unaligned landmark', () => {
    const o = outline({
      stats: stats({ error_count: 1 }),
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'run the suite' }),
        turn({
          uuid: 'a1', kind: 'assistant', label: 'working',
          segment_uuids: ['a1', 'seg-2'], tools: [{ name: 'exec', is_error: true }],
        }),
      ],
      landmarks: [{
        landmark_key: 'cbk.e1#tool_error', block_key: 'cbk.e1', uuid: 'seg-2',
        parent_uuid: 'a1', kind: 'tool_error', label: 'exec', ts: null,
      }],
    });
    act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'seg-2' }); });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current).toHaveLength(1);
    expect(current[0].textContent).toContain('run the suite');
    expect(current[0].textContent).not.toContain('tool error');
  });

  // #492 case 2 — a cache-rebuild jump can land on a later member of a
  // sidechain bucket. That member has no row of its own, but its bucket is the
  // nearest outline ancestor and must keep the one-current-row invariant.
  it('a pin on a non-root sidechain member marks its subagent bucket', () => {
    const o = outline({
      subagent_meta: { sk1: { kind: 'implementer', description: 'Server work' } },
      turns: [
        turn({ uuid: 'h1', kind: 'human', label: 'build it' }),
        turn({
          uuid: 'sa-root', kind: 'human', label: 'task', subagent_key: 'sk1',
          is_sidechain: true,
        }),
        turn({
          uuid: 'sa-target', kind: 'assistant', label: '', subagent_key: 'sk1',
          parent_uuid: 'sa-parent-member', is_sidechain: true,
        }),
      ],
    });
    act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'sa-target' }); });
    const { container } = render(<OutlinePanel sessionId="s1" outline={o} />);
    const current = container.querySelectorAll('[aria-current="true"]');
    expect(current).toHaveLength(1);
    expect(current[0].classList.contains('conv-outline-entry--subagent')).toBe(true);
    expect(current[0].textContent).toContain('Server work');
  });

  // C-8 — the noun the phrase pluralized and the noun it did not.
  it('says "in 1 turn" rather than "in 1 turns"', () => {
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={outline({
        stats: stats({ error_count: 3 }),
        turns: [
          turn({ uuid: 'h1', kind: 'human', label: 'go' }),
          turn({
            uuid: 'a1', kind: 'assistant', label: 'working',
            segment_uuids: ['a1', 'seg-2'], tools: [{ name: 'exec', is_error: true }],
          }),
        ],
        landmarks: [
          {
            landmark_key: 'cbk.e1#tool_error', block_key: 'cbk.e1', uuid: 'a1',
            parent_uuid: 'a1', kind: 'tool_error', label: 'exec', ts: null,
          },
          {
            landmark_key: 'cbk.e2#tool_error', block_key: 'cbk.e2', uuid: 'seg-2',
            parent_uuid: 'a1', kind: 'tool_error', label: 'exec', ts: null,
          },
        ],
      })} />,
    );
    const row = container.querySelector('.conv-outline-stat-kv--errors')!;
    expect(row.textContent).toContain('3 errors in 1 turn');
    expect(row.textContent).not.toContain('1 turns');
  });

  it('says "1 turn · 1 yours" in the headline of a one-turn conversation', () => {
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={outline({
        stats: stats({
          turns: { total: 1, human: 1, assistant: 0, tool_result: 0, meta: 0 },
        }),
        turns: [turn({ uuid: 'h1', kind: 'human', label: 'go' })],
      })} />,
    );
    const headline = container.querySelector('.conv-outline-stats-headline')!;
    expect(headline.textContent).toContain('1 turn ·');
    expect(headline.textContent).not.toContain('1 turns');
  });

  // A sibling entry point the enumeration did not name: the per-rebuild jump
  // list dispatched its own OPEN_CONVERSATION inline, so it was the one rail row
  // that skipped the focus-mode unhide check every other rail row performs.
  it('resets a focus mode that would hide the turn a cache-rebuild row jumps to', () => {
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'edits' }); });
    const { container } = render(
      <OutlinePanel sessionId="s1" outline={outline({
        stats: stats({
          cache_failures: {
            count: 1, tokens_recreated: 10, est_wasted_usd: 0.12,
            rebuilds: [{ uuid: 'a1', ts: null, tokens_recreated: 10, est_wasted_usd: 0.12, subagent_key: null }],
          },
        }),
        turns: [
          turn({ uuid: 'h1', kind: 'human', label: 'go' }),
          turn({ uuid: 'a1', kind: 'assistant', label: 'working',
                 cache_failure: { tokens_recreated: 10, prev_cached: 100, est_wasted_usd: 0.12 },
                 tools: [{ name: 'exec', is_error: false }] }),
        ],
      })} />,
    );
    act(() => { fireEvent.click(container.querySelector('.conv-rebuild-jump')!); });
    expect(getState().convFocusMode).toBe('all');
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'a1' });
  });
});
