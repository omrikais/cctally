import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationReader } from './ConversationReader';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import type { Envelope } from '../types/envelope';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymapForTests,
} from '../store/keymap';
import { installIntersectionObserverStub } from '../test-utils/intersectionObserver';
import type { ConversationItem, ConversationOutline, OutlineTurn } from '../types/conversation';

function makeItem(over: Partial<ConversationItem> & { uuid: string; kind?: ConversationItem['kind']; is_sidechain?: boolean }): ConversationItem {
  const { uuid, kind = 'human', is_sidechain = false, ...rest } = over;
  return {
    kind,
    anchor: { session_id: 's', uuid, id: 0 },
    member_uuids: [uuid],
    ts: 't',
    text: uuid,
    blocks: [],
    is_sidechain,
    subagent_key: is_sidechain ? 'k1' : null,
    parent_uuid: null,
    ...rest,
  } as ConversationItem;
}

function detail(items: ConversationItem[], next_after: number | null = null) {
  return {
    session_id: 's',
    project_label: 'proj',
    git_branch: 'main',
    started_utc: '2026-01-01T00:00:00Z',
    last_activity_utc: '2026-01-01T02:00:00Z',
    cost_usd: 3.5,
    models: ['claude-opus-4'],
    items,
    page: { next_after, has_more: next_after != null },
  };
}

function mockFetchOnce(body: unknown, status = 200) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: status < 400, status, json: async () => body,
  } as Response);
}

// ---- #175 F4 reader-scroll helpers ------------------------------------
// jsdom doesn't lay out, so set the scroll metrics by hand. scrollTop is
// writable on an element; clientHeight/scrollHeight are getters we override.
function setScroll(el: HTMLElement, m: { scrollTop: number; clientHeight: number; scrollHeight: number }) {
  el.scrollTop = m.scrollTop;
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: m.clientHeight });
  Object.defineProperty(el, 'scrollHeight', { configurable: true, value: m.scrollHeight });
}
// Drive a live SSE tick: bump the store snapshot's generated_at so the
// hook's tail-poll effect fires.
function bumpSnapshot(tag: string) {
  updateSnapshot({ generated_at: tag } as Envelope);
}
// jsdom doesn't implement Element.prototype.scrollTo, so vi.spyOn can't attach
// to a missing property — define a no-op first, then spy on it.
function spyScrollTo() {
  if (typeof Element.prototype.scrollTo !== 'function') {
    Element.prototype.scrollTo = () => {};
  }
  return vi.spyOn(Element.prototype, 'scrollTo').mockImplementation(() => {});
}

beforeEach(() => {
  _resetForTests();
  _resetKeymapForTests();
  globalThis.fetch = vi.fn();
  installIntersectionObserverStub();
});
afterEach(() => {
  uninstallGlobalKeydown();
  _resetForTests();
  vi.restoreAllMocks();
});

describe('ConversationReader', () => {
  it('renders the header and groups parallel subagents into separate threads', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'a1', is_sidechain: true, subagent_key: 'A', text: 'Audit A' } as never),
      makeItem({ uuid: 'b1', is_sidechain: true, subagent_key: 'B', text: 'Audit B' } as never),
      makeItem({ uuid: 'a2', is_sidechain: true, subagent_key: 'A' } as never),
      makeItem({ uuid: 'b2', is_sidechain: true, subagent_key: 'B' } as never),
    ]));
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader-body')).not.toBeNull());

    expect(container.querySelector('.conv-reader-meta')!.textContent).toContain('$3.50');
    const body = container.querySelector('.conv-reader-body')!;
    expect(body.querySelector('[data-uuid="h1"]')).not.toBeNull();
    // TWO separate subagent disclosures (not one fused group).
    const groups = body.querySelectorAll('details.conv-sidechain');
    expect(groups).toHaveLength(2);
    expect(groups[0].querySelector('summary')!.textContent).toContain('Audit A');
    expect(groups[1].querySelector('summary')!.textContent).toContain('Audit B');
  });

  it('threads subagent_meta from the detail payload into the subagent card (#166)', async () => {
    // A main human + one subagent thread keyed "aaaa1111", with a top-level
    // subagent_meta map. The reader must hand the matching entry to the
    // SidechainGroup, which surfaces the kind in the eyebrow — catching a broken
    // ConversationReader → SidechainGroup hand-off, not just the child unit.
    mockFetchOnce({
      ...detail([
        makeItem({ uuid: 'h1' }),
        makeItem({ uuid: 'a1', is_sidechain: true, subagent_key: 'aaaa1111', text: 'Audit A' } as never),
        makeItem({ uuid: 'a2', is_sidechain: true, subagent_key: 'aaaa1111' } as never),
      ]),
      subagent_meta: { aaaa1111: { kind: 'Explore', total_tokens: 1, status: 'completed' } },
    });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader-body')).not.toBeNull());
    await waitFor(() =>
      expect(document.querySelector('.conv-sidechain-kindname')!.textContent).toContain('Explore'));
  });

  it('jumps to a target message: pages until loaded, scrolls, and flashes the highlight', async () => {
    // Page 1 has h1 only, with more to come; page 2 carries the target.
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })], 2));
    mockFetchOnce(detail([makeItem({ uuid: 'target', member_uuids: ['target', 'targetFrag'] } as never)], null));

    const scrollSpy = vi
      .spyOn(Element.prototype, 'scrollIntoView')
      .mockImplementation(() => {});

    // Set the jump for this session BEFORE rendering the reader.
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'targetFrag' } });

    const { container } = render(<ConversationReader sessionId="s" />);

    // The reader pages until the target's member_uuids include 'targetFrag',
    // then scrolls it into view and marks it jumped.
    await waitFor(() => {
      const el = container.querySelector('[data-uuid="target"]');
      expect(el).not.toBeNull();
    });
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    const target = container.querySelector('[data-uuid="target"]')!;
    expect(target.classList.contains('conv-item--jumped')).toBe(true);
  });

  it('gives up on a jump when pagination exhausts on an empty terminal page (no infinite loop)', async () => {
    // Page 1 returns h1 + a cursor (has_more true). Page 2 — the after=<id>
    // fetch — is the empty terminal page: items: [], next_after: null. The
    // target uuid never appears anywhere. With Fix 1 the give-up clear
    // fires when hasMore transitions to false even though items.length never
    // grew on the terminal page (the regression this pins).
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })], 2));
    mockFetchOnce({
      session_id: 's',
      project_label: 'proj',
      git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z',
      last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 3.5,
      models: ['claude-opus-4'],
      items: [],
      page: { next_after: null, has_more: false },
    });

    const scrollSpy = vi
      .spyOn(Element.prototype, 'scrollIntoView')
      .mockImplementation(() => {});

    // Jump targets a uuid that never lands in any item.
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'never-appears' } });

    render(<ConversationReader sessionId="s" />);

    // The jump clears via the give-up branch once paging is exhausted.
    await waitFor(() => expect(getState().conversationJump).toBeNull());

    // No target was ever found → no scroll, and fetch ran a bounded number
    // of times (page 1 + page 2 = 2; allow a small constant for re-renders).
    expect(scrollSpy).not.toHaveBeenCalled();
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeLessThanOrEqual(4);
  });

  it('does not clear a cross-session jump while detail still belongs to the prior session (cross-session guard)', async () => {
    // The reader is reused across session switches (ConversationsView mounts it
    // at a fixed position), so for one render pass it can hold the PRIOR
    // session's detail while sessionId + jump already point at the NEW session.
    // Modelled here as a stable window: the reader is asked for session 'B'
    // (jump 'B/targetB'), but the loaded detail reports session 's' with no
    // more pages and without targetB. Without the detail.session_id===sessionId
    // guard the jump effect runs against the 's' detail, finds nothing, and
    // (s having no more pages) clears the jump prematurely so 'B' never scrolls.
    // With the guard it short-circuits and leaves the jump set, waiting for 'B'.
    mockFetchOnce({
      session_id: 's', project_label: 'proj', git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 1, models: ['claude-opus-4'],
      items: [makeItem({ uuid: 'a1' })],
      page: { next_after: null, has_more: false },   // s: fully loaded, no more pages
    });
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});

    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'B', jump: { session_id: 'B', uuid: 'targetB' } });
    render(<ConversationReader sessionId="B" />);
    // Let the loaded-'s' detail land and the jump effect run to completion.
    await act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); });

    // The guard kept the jump alive (it would resolve once 'B' itself loads);
    // the give-up branch did NOT fire against the cross-session 's' detail.
    expect(getState().conversationJump).toEqual({ session_id: 'B', uuid: 'targetB' });
    expect(scrollSpy).not.toHaveBeenCalled();
  });

  it('resolves a jump into a different session after the new session loads (cross-session guard, full flow)', async () => {
    // Behavior-preservation guard: once the reused reader's detail catches up to
    // the new session 'B' (which carries the jump target), the jump resolves —
    // it scrolls + flashes 'B's target rather than staying stuck.
    mockFetchOnce(detail([makeItem({ uuid: 'a1' })], null));   // session 's': one item, no more
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});

    const { container, rerender } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="a1"]')).not.toBeNull());

    // Queue 'B' page-1 (carries the jump target), then open the hit + switch.
    mockFetchOnce({
      session_id: 'B', project_label: 'proj', git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 1, models: ['claude-opus-4'],
      items: [{
        kind: 'human', anchor: { session_id: 'B', uuid: 'targetB', id: 0 },
        member_uuids: ['targetB'], ts: 't', text: 'targetB', blocks: [],
        is_sidechain: false, subagent_key: null, parent_uuid: null,
      }],
      page: { next_after: null, has_more: false },
    });
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'B', jump: { session_id: 'B', uuid: 'targetB' } }); });
    rerender(<ConversationReader sessionId="B" />);

    // 'B' lands; the jump resolves against 'B' (scroll + flash), not cleared early.
    await waitFor(() => expect(container.querySelector('[data-uuid="targetB"]')).not.toBeNull());
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    expect(container.querySelector('[data-uuid="targetB"]')!.classList.contains('conv-item--jumped')).toBe(true);
  });

  it('jumps to a FOLDED tool_result uuid: scrolls the owning assistant turn (#160 + #164)', async () => {
    // The kernel folds a tool_result row's uuid ('u1') into its owning turn's
    // member_uuids. A jump targeting 'u1' must resolve to the turn element
    // (data-uuid = the turn's anchor 'a1'), since getItemRef maps every
    // member_uuids entry — including the folded result uuid — to that element.
    mockFetchOnce(detail([
      {
        kind: 'assistant',
        anchor: { session_id: 's', uuid: 'a1', id: 1 },
        member_uuids: ['a1', 'u1'], // u1 = the folded tool_result uuid
        ts: 't',
        text: 'paired turn',
        model: 'claude-opus-4',
        is_sidechain: false,
        subagent_key: null,
        parent_uuid: null,
        cost_usd: 0.01,
        blocks: [
          { kind: 'text', text: 'paired turn' },
          {
            kind: 'tool_call',
            name: 'Read',
            input_summary: '{}',
            preview: '/x.py',
            tool_use_id: 't1',
            result: { text: 'BODY', truncated: false, is_error: false },
          },
        ],
      } as ConversationItem,
    ]));

    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'u1' } });
    const { container } = render(<ConversationReader sessionId="s" />);

    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    const turn = container.querySelector('[data-uuid="a1"]')!;
    expect(turn).not.toBeNull();
    expect(turn.classList.contains('conv-item--jumped')).toBe(true);
    await waitFor(() => expect(getState().conversationJump).toBeNull());
  });

  it('header leads with the derived title, not project_label', async () => {
    // First human item carries the prompt; project_label is "proj".
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1', text: 'design the conversation reader\nsecond line' }),
      makeItem({ uuid: 'a1', kind: 'assistant', text: 'sure', model: 'claude-opus-4', cost_usd: 0.01 } as never),
    ]));
    render(<ConversationReader sessionId="s" />);
    expect(await screen.findByText('design the conversation reader')).toBeInTheDocument();
    await waitFor(() =>
      expect(document.querySelector('.conv-reader-title')!.textContent).toBe('design the conversation reader'),
    );
    // The project label is demoted into the meta line.
    expect(document.querySelector('.conv-reader-meta')!.textContent).toContain('proj');
  });

  it('header falls back to project_label when the opening human is a system marker', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'm1', text: '<command-name>clear</command-name>' }),
    ]));
    render(<ConversationReader sessionId="s" />);
    await waitFor(() =>
      expect(document.querySelector('.conv-reader-title')!.textContent).toBe('proj'),
    );
  });

  // #186 — belt-and-suspenders: the title skips ANY line wrapped entirely in a
  // command-*/local-command-* family tag, even an UNKNOWN one not in MARKER_TAGS
  // (the strict isSystemMarker would NOT skip `local-command-future`). The title
  // then falls through to the next real prompt, never poisoned by future
  // unrecognized plumbing.
  it('header skips an unknown command-family plumbing line and uses the next real prompt', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'm1', text: '<local-command-future>x</local-command-future>' }),
      makeItem({ uuid: 'h1', text: 'the real first prompt' }),
    ]));
    render(<ConversationReader sessionId="s" />);
    await waitFor(() =>
      expect(document.querySelector('.conv-reader-title')!.textContent).toBe('the real first prompt'),
    );
  });

  it('renders a styled selection-empty / loading state, not bare text', async () => {
    // First page never resolves → the loading state shows the styled .conv-state.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise(() => {}));
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader--loading')).not.toBeNull());
    expect(container.querySelector('.conv-state')).not.toBeNull();
    const glyph = container.querySelector('.conv-state-glyph')!;
    expect(glyph).not.toBeNull();
    // C3: the loading state glyph is now an inline SVG (not the ⏳ emoji).
    expect(glyph.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(glyph.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
    expect(container.querySelector('.conv-state-title')).not.toBeNull();
  });

  it('rise-animates each top-level item once on first appearance, not on re-render (G1 §4b)', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'a1', kind: 'assistant', text: 'reply', model: 'claude-opus-4', cost_usd: 0.01 } as never),
    ]));
    const { container, rerender } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());

    const first = container.querySelector('[data-uuid="h1"]')!;
    expect(first.className).toMatch(/conv-rise/);
    // A re-render (same props) must NOT re-animate the already-painted item:
    // the seen-Set ref marks it after commit.
    rerender(<ConversationReader sessionId="s" />);
    expect(container.querySelector('[data-uuid="h1"]')!.className).not.toMatch(/conv-rise/);
  });

  it('staggers the first content page with a per-index animationDelay (idx*40ms, G1 §4b)', async () => {
    // The first CONTENT render (seen-Set still empty) must stagger; the loading
    // branch that renders before `detail` resolves must NOT consume "first page".
    // Three top-level items, no active jump → delays 0/40/80ms at indices 0/1/2.
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'a1', kind: 'assistant', text: 'reply', model: 'claude-opus-4', cost_usd: 0.01 } as never),
      makeItem({ uuid: 'h2' }),
    ]));
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());

    const delayOf = (uuid: string) =>
      (container.querySelector(`[data-uuid="${uuid}"]`) as HTMLElement).style.animationDelay;
    expect(delayOf('h1')).toBe('0ms');
    expect(delayOf('a1')).toBe('40ms');
    expect(delayOf('h2')).toBe('80ms');
  });

  it('the active jump target gets conv-item--jumped WITHOUT conv-rise (Codex P2)', async () => {
    // Jump targets a page-1 uuid set BEFORE first paint; the render-time
    // classifier must deny it conv-rise so only the flash runs on that element.
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'target', member_uuids: ['target', 'targetFrag'] } as never),
    ], null));
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'targetFrag' } });

    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    const el = container.querySelector('[data-uuid="target"]')!;
    expect(el.classList.contains('conv-item--jumped')).toBe(true);
    // The two animations never run on one element.
    expect(el.className).not.toMatch(/conv-rise/);
  });

  it('auto-expands a collapsed subagent thread, scrolls, and highlights when jumping to a member', async () => {
    // Page 1: a main item + a collapsed subagent thread 'A' (sa1 root, sa2 member).
    // No more pages. The jump targets sa2, which lives inside the collapsed thread.
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'sa1', is_sidechain: true, subagent_key: 'A', text: 'Audit A' } as never),
      makeItem({ uuid: 'sa2', is_sidechain: true, subagent_key: 'A' } as never),
    ]));
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});

    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'sa2' } });
    const { container } = render(<ConversationReader sessionId="s" />);

    // The owning subagent thread auto-expands.
    await waitFor(() => {
      const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement | null;
      expect(det?.open).toBe(true);
    });
    // The target member scrolls into view, flashes, and the jump clears.
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    expect(container.querySelector('[data-uuid="sa2"]')!.classList.contains('conv-item--jumped')).toBe(true);
    await waitFor(() => expect(getState().conversationJump).toBeNull());
  });

  it('a find-jump to a focus-mode-hidden turn resets the mode to `all`, then lands the jump (spec §4)', async () => {
    // Focus mode `prompts` keeps human turns and hides assistant turns. A find
    // match on the hidden assistant turn 'a1' must escape the filter the same way
    // jump-to-next does: reset to `all`, re-render, then scroll + flash. Without
    // the jump-effect mode-reset the target never renders (it coalesces into a
    // `hidden_run` marker, ref-less), so the jump silently no-ops and clears once
    // pagination is exhausted (the regression this pins).
    //
    // NOTE: the OPEN_CONVERSATION reducer ONLY blanket-resets the focus mode on a
    // GENUINE session switch (different sessionId). A same-session find-jump (this
    // case) preserves the mode by design — the per-jump hidden check is the
    // caller/effect's job — so the reset proven here can ONLY come from the jump
    // effect's mode-hidden fallback. Hence we select the session FIRST, so the
    // find-jump below is same-session and the reducer does not mask the fix.
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1', kind: 'human', text: 'prompt one' }),
      makeItem({ uuid: 'a1', kind: 'assistant', text: '', blocks: [] } as never),
    ]));
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});

    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' }); });
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());
    // The assistant target is hidden behind a hidden_run marker in prompts mode.
    expect(container.querySelector('[data-uuid="a1"]')).toBeNull();

    // FindBar drives a same-session OPEN_CONVERSATION jump (expand_details set
    // when the match was inside a tool/thinking block).
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'a1', expand_details: false } }); });

    // The mode escapes back to `all` so the hidden assistant turn renders again.
    await waitFor(() => expect(getState().convFocusMode).toBe('all'));
    await waitFor(() => {
      expect(container.querySelector('[data-uuid="a1"]')).not.toBeNull();
    });
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    expect(container.querySelector('[data-uuid="a1"]')!.classList.contains('conv-item--jumped')).toBe(true);
    await waitFor(() => expect(getState().conversationJump).toBeNull());
  });
});

describe('ConversationReader live-tail scroll (#175 F4)', () => {
  // Render a fully-paged conversation (next_after null), then drive a live tail
  // append by bumping the snapshot + queueing a tail fetch. Returns the body.
  async function renderFullyPaged(items: ConversationItem[]) {
    mockFetchOnce(detail(items, null));
    const utils = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-body')).not.toBeNull());
    await waitFor(() =>
      expect(utils.container.querySelectorAll('.conv-reader-thread > *').length).toBe(items.length));
    const body = utils.container.querySelector('.conv-reader-body') as HTMLElement;
    return { ...utils, body };
  }

  // Append one new turn via the live tail (the tail response stays fully paged).
  async function appendLiveItem(newUuid: string) {
    mockFetchOnce(detail([makeItem({ uuid: newUuid })], null));
    await act(async () => {
      bumpSnapshot(`t-${newUuid}`);
      // let the tail poll fetch + setState flush
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
  }

  it('live append sticks to bottom when already at bottom', async () => {
    const { body } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    setScroll(body, { scrollTop: 990, clientHeight: 10, scrollHeight: 1000 }); // at bottom
    fireEvent.scroll(body);
    const scrollToSpy = spyScrollTo();

    await appendLiveItem('live1');
    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    // No pill while stuck to the bottom.
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });

  it('live append while scrolled up preserves position and shows the pill', async () => {
    const { body } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 }); // scrolled up
    fireEvent.scroll(body);
    const scrollToSpy = spyScrollTo();

    await appendLiveItem('live1');
    // Did not auto-scroll; surfaced the pill with a count.
    expect(scrollToSpy).not.toHaveBeenCalled();
    const pill = await screen.findByRole('button', { name: /new/i });
    expect(pill).toBeInTheDocument();
    expect(pill.textContent).toMatch(/1 new/);

    // Clicking the pill scrolls to bottom and clears it.
    fireEvent.click(pill);
    expect(scrollToSpy).toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });

  it('the final PAGINATION append (was hasMore) shows no pill and no stick (P0 discriminator)', async () => {
    // Page 1 has a cursor (hasMore true). Render in conversations view with the
    // keymap installed so `j` at the last item triggers loadMore -> the FINAL
    // pagination page (next_after null). prevHasMore was TRUE on that append, so
    // it must NOT be treated as a live append.
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })], 2));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());

    const body = container.querySelector('.conv-reader-body') as HTMLElement;
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 }); // scrolled up
    fireEvent.scroll(body);
    const scrollToSpy = spyScrollTo();

    // The final page lands via loadMore (j at the single, last item).
    mockFetchOnce(detail([makeItem({ uuid: 'h2' })], null));
    await act(async () => {
      fireEvent.keyDown(document, { key: 'j' });
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="h2"]')).not.toBeNull());

    // A pagination append must neither stick nor raise a pill.
    expect(scrollToSpy).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });

  it('clears a stale "↓ N new" pill on a session switch (#175 P1)', async () => {
    // Render convo A fully-paged, scroll up, drive a live append so the pill
    // appears. Then switch the reused reader to convo B (new sessionId + B's
    // detail). Without the per-session pill reset the stale pill survives the
    // switch until the user scrolls B to the bottom; with it the pill is gone
    // the moment B loads.
    const { body, rerender, container } =
      await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 }); // scrolled up
    fireEvent.scroll(body);
    spyScrollTo();

    await appendLiveItem('live1');
    // Pill is up on convo A.
    expect(await screen.findByRole('button', { name: /new/i })).toBeInTheDocument();

    // Switch the reused reader to convo B; queue B's page-1 detail.
    mockFetchOnce({
      session_id: 'B', project_label: 'projB', git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 2, models: ['claude-opus-4'],
      items: [makeItem({ uuid: 'b1' })],
      page: { next_after: null, has_more: false },
    });
    await act(async () => {
      rerender(<ConversationReader sessionId="B" />);
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="b1"]')).not.toBeNull());

    // The stale pill must be gone on B (per-session reset cleared newCount).
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });

  it('a pagination append followed by a live tail append shows the pill (sequence guard)', async () => {
    // After the final pagination page lands (hasMore flips false), the NEXT
    // growth — a live tail append — must be treated as live (pill appears).
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })], 2));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());
    const body = container.querySelector('.conv-reader-body') as HTMLElement;
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 });
    fireEvent.scroll(body);
    const scrollToSpy = spyScrollTo();

    // Final pagination page → hasMore false; no pill yet.
    mockFetchOnce(detail([makeItem({ uuid: 'h2' })], null));
    await act(async () => {
      fireEvent.keyDown(document, { key: 'j' });
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="h2"]')).not.toBeNull());
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();

    // Now a live tail append — prevHasMore is false → pill.
    mockFetchOnce(detail([makeItem({ uuid: 'live1' })], null));
    // Re-pin the scroll metrics (jsdom append doesn't recompute them) so the
    // layout effect still reads "scrolled up".
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 });
    fireEvent.scroll(body);
    await act(async () => {
      bumpSnapshot('t-live1');
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    const pill = await screen.findByRole('button', { name: /new/i });
    expect(pill).toBeInTheDocument();
    expect(scrollToSpy).not.toHaveBeenCalled();
  });
});

describe('ConversationReader floating "↑ Top of turn" button (#176)', () => {
  // jsdom never lays out, so getBoundingClientRect returns all-zeros. The
  // visibility decision keys on rects, so stub each element's rect by hand:
  // the body (the scroller) plus each top-level thread child (the turns). The
  // helper installs a single prototype spy that dispatches on element identity.
  function stubRects(
    map: Map<Element, { top: number; bottom: number }>,
  ) {
    return vi
      .spyOn(Element.prototype, 'getBoundingClientRect')
      .mockImplementation(function (this: Element) {
        const r = map.get(this) ?? { top: 0, bottom: 0 };
        return {
          top: r.top, bottom: r.bottom, left: 0, right: 0,
          width: 0, height: r.bottom - r.top, x: 0, y: r.top, toJSON() {},
        } as DOMRect;
      });
  }

  async function renderFullyPaged(items: ConversationItem[]) {
    mockFetchOnce(detail(items, null));
    const utils = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-body')).not.toBeNull());
    await waitFor(() =>
      expect(utils.container.querySelectorAll('.conv-reader-thread > *').length).toBe(items.length));
    const body = utils.container.querySelector('.conv-reader-body') as HTMLElement;
    const thread = utils.container.querySelector('.conv-reader-thread') as HTMLElement;
    return { ...utils, body, thread };
  }

  it('shows no button when the first turn is at the top of the viewport', async () => {
    const { body, thread } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    // Body top at 0; the first turn's top is flush with it (not scrolled past).
    stubRects(new Map<Element, { top: number; bottom: number }>([
      [body, { top: 0, bottom: 600 }],
      [thread.children[0], { top: 0, bottom: 1000 }],
      [thread.children[1], { top: 1000, bottom: 1100 }],
    ]));
    fireEvent.scroll(body);
    expect(screen.queryByRole('button', { name: /jump to the start of this turn/i })).toBeNull();
  });

  it('shows the button when scrolled deep into a tall first turn (top off by > 160px)', async () => {
    const { body, thread } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    // Body top at 0; the first turn's top is 300px above the body top, so it's
    // the block under the viewport top AND scrolled past the 160px threshold.
    stubRects(new Map<Element, { top: number; bottom: number }>([
      [body, { top: 0, bottom: 600 }],
      [thread.children[0], { top: -300, bottom: 1000 }],
      [thread.children[1], { top: 1000, bottom: 1100 }],
    ]));
    fireEvent.scroll(body);
    expect(await screen.findByRole('button', { name: /jump to the start of this turn/i })).toBeInTheDocument();
  });

  it('does NOT show the button when the current turn is barely scrolled (under the threshold)', async () => {
    const { body, thread } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    // First turn's top only 100px above the body top — under the 160px floor.
    stubRects(new Map<Element, { top: number; bottom: number }>([
      [body, { top: 0, bottom: 600 }],
      [thread.children[0], { top: -100, bottom: 1000 }],
      [thread.children[1], { top: 1000, bottom: 1100 }],
    ]));
    fireEvent.scroll(body);
    expect(screen.queryByRole('button', { name: /jump to the start of this turn/i })).toBeNull();
  });

  it('clicking the button scrolls the current turn back to its start and hides it', async () => {
    const { body, thread } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    stubRects(new Map<Element, { top: number; bottom: number }>([
      [body, { top: 0, bottom: 600 }],
      [thread.children[0], { top: -300, bottom: 1000 }],
      [thread.children[1], { top: 1000, bottom: 1100 }],
    ]));
    fireEvent.scroll(body);
    const btn = await screen.findByRole('button', { name: /jump to the start of this turn/i });

    const scrollIntoViewSpy = vi
      .spyOn(thread.children[0], 'scrollIntoView')
      .mockImplementation(() => {});
    fireEvent.click(btn);
    // Scrolls the turn under the viewport top back to its start.
    expect(scrollIntoViewSpy).toHaveBeenCalledWith(expect.objectContaining({ block: 'start' }));
    // And the button hides immediately.
    expect(screen.queryByRole('button', { name: /jump to the start of this turn/i })).toBeNull();
  });

  it('hides the button on a session switch (#176 reset)', async () => {
    const { body, thread, rerender, container } =
      await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    stubRects(new Map<Element, { top: number; bottom: number }>([
      [body, { top: 0, bottom: 600 }],
      [thread.children[0], { top: -300, bottom: 1000 }],
      [thread.children[1], { top: 1000, bottom: 1100 }],
    ]));
    fireEvent.scroll(body);
    expect(await screen.findByRole('button', { name: /jump to the start of this turn/i })).toBeInTheDocument();

    // Switch the reused reader to convo B.
    mockFetchOnce({
      session_id: 'B', project_label: 'projB', git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 2, models: ['claude-opus-4'],
      items: [makeItem({ uuid: 'b1' })],
      page: { next_after: null, has_more: false },
    });
    await act(async () => {
      rerender(<ConversationReader sessionId="B" />);
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="b1"]')).not.toBeNull());

    // The stale jump-top button must be gone on B (per-session reset).
    expect(screen.queryByRole('button', { name: /jump to the start of this turn/i })).toBeNull();
  });
});

describe('ConversationReader keyboard navigation (G3)', () => {
  // The reader's keymap is `view:'conversations'`-scoped; the conversations
  // view is entered via OPEN_CONVERSATION (sets view + selection).
  async function renderInConversations(items: ConversationItem[], next_after: number | null = null) {
    mockFetchOnce(detail(items, next_after));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-thread')).not.toBeNull());
    const thread = utils.container.querySelector('.conv-reader-thread') as HTMLElement;
    return { ...utils, thread };
  }
  const press = (key: string) => fireEvent.keyDown(document, { key });

  it('j/k move the focused-turn cursor and clamp at both ends', async () => {
    const { thread } = await renderInConversations([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'a1', kind: 'assistant', text: 'r', model: 'm', cost_usd: 0.01 } as never),
      makeItem({ uuid: 'h2' }),
    ]);
    // Starts at 0.
    expect(thread.children[0]).toHaveClass('conv-item--focused');
    press('j');
    expect(thread.children[1]).toHaveClass('conv-item--focused');
    expect(thread.children[0]).not.toHaveClass('conv-item--focused');
    press('j');
    expect(thread.children[2]).toHaveClass('conv-item--focused');
    press('j'); // clamp at the last child
    expect(thread.children[2]).toHaveClass('conv-item--focused');
    press('k');
    expect(thread.children[1]).toHaveClass('conv-item--focused');
    press('k');
    press('k'); // clamp at 0
    expect(thread.children[0]).toHaveClass('conv-item--focused');
  });

  it('[ collapses and ] expands every <details> in the thread', async () => {
    const { thread } = await renderInConversations([
      makeItem({ uuid: 's1', is_sidechain: true, subagent_key: 'A', text: 'Audit A' } as never),
      makeItem({ uuid: 's2', is_sidechain: true, subagent_key: 'A' } as never),
    ]);
    // The thread renders at least one <details> (the subagent disclosure).
    const allDetails = () => Array.from(thread.querySelectorAll('details'));
    expect(allDetails().length).toBeGreaterThan(0);
    press(']');
    allDetails().forEach((d) => expect((d as HTMLDetailsElement).open).toBe(true));
    press('[');
    allDetails().forEach((d) => expect((d as HTMLDetailsElement).open).toBe(false));
  });

  it('g scrolls the reader body to top and resets the cursor to 0', async () => {
    const scrollTo = vi.fn();
    const { container, thread } = await renderInConversations([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'h2' }),
      makeItem({ uuid: 'h3' }),
    ]);
    const body = container.querySelector('.conv-reader-body') as HTMLElement;
    body.scrollTo = scrollTo as never;
    press('j');
    press('j');
    expect(thread.children[2]).toHaveClass('conv-item--focused');
    press('g');
    expect(scrollTo).toHaveBeenCalled();
    expect(thread.children[0]).toHaveClass('conv-item--focused');
  });

  it('bindings are inert while a modal is open', async () => {
    const { thread } = await renderInConversations([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'h2' }),
    ]);
    act(() => { dispatch({ type: 'OPEN_MODAL', kind: 'session' }); });
    press('j');
    // No move from index 0 while a modal owns the keys.
    expect(thread.children[0]).toHaveClass('conv-item--focused');
    expect(thread.children[1]).not.toHaveClass('conv-item--focused');
  });

  it('bindings are inert while input-mode is active', async () => {
    const { thread } = await renderInConversations([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'h2' }),
    ]);
    act(() => { dispatch({ type: 'SET_INPUT_MODE', mode: 'search' }); });
    press('j');
    expect(thread.children[0]).toHaveClass('conv-item--focused');
    expect(thread.children[1]).not.toHaveClass('conv-item--focused');
  });

  it('does not fire on the dashboard view (view-scoped binding)', async () => {
    // Mount in conversations, then leave to the dashboard: the binding is
    // view:'conversations'-gated so j must not move the cursor.
    const { thread } = await renderInConversations([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'h2' }),
    ]);
    act(() => { dispatch({ type: 'SET_VIEW', view: 'dashboard' }); });
    press('j');
    expect(thread.children[0]).toHaveClass('conv-item--focused');
    expect(thread.children[1]).not.toHaveClass('conv-item--focused');
  });

  // #177 S5 — the `o` key toggles the outline open flag; the toggle button in
  // the reader head mirrors the flag via aria-pressed and dispatches the same.
  it('o toggles the outline open flag', async () => {
    await renderInConversations([makeItem({ uuid: 'h1' })]);
    const before = getState().convOutlineOpen;
    press('o');
    expect(getState().convOutlineOpen).toBe(!before);
    press('o');
    expect(getState().convOutlineOpen).toBe(before);
  });

  it('o is inert while a modal is open (modal guard)', async () => {
    await renderInConversations([makeItem({ uuid: 'h1' })]);
    const before = getState().convOutlineOpen;
    act(() => { dispatch({ type: 'OPEN_MODAL', kind: 'session' }); });
    press('o');
    expect(getState().convOutlineOpen).toBe(before); // unchanged
  });

  it('the reader-head outline toggle reflects + flips the open flag', async () => {
    const { container } = await renderInConversations([makeItem({ uuid: 'h1' })]);
    const btn = container.querySelector<HTMLButtonElement>('.conv-outline-toggle')!;
    expect(btn).not.toBeNull();
    const before = getState().convOutlineOpen;
    expect(btn.getAttribute('aria-pressed')).toBe(String(before));
    fireEvent.click(btn);
    expect(getState().convOutlineOpen).toBe(!before);
    await waitFor(() => expect(btn.getAttribute('aria-pressed')).toBe(String(!before)));
  });
});

// ---- #177 S5 §5 — focus modes + jump-to-next ------------------------------
function oTurn(over: Partial<OutlineTurn> & { uuid: string; kind: OutlineTurn['kind'] }): OutlineTurn {
  return {
    ts: null, label: over.uuid, member_uuids: [over.uuid], subagent_key: null,
    parent_uuid: null, is_sidechain: false, ...over,
  };
}

describe('ConversationReader focus modes (#177 S5 §5)', () => {
  async function renderWithOutline(items: ConversationItem[], outline: ConversationOutline) {
    mockFetchOnce(detail(items));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-thread')).not.toBeNull());
    const thread = utils.container.querySelector('.conv-reader-thread') as HTMLElement;
    return { ...utils, thread };
  }
  const press = (key: string) => fireEvent.keyDown(document, { key });

  const baseOutline = (turns: OutlineTurn[], errorCount = 0): ConversationOutline => ({
    session_id: 's',
    stats: {
      turns: { total: turns.length, human: 0, assistant: 0, tool_result: 0, meta: 0 },
      tool_counts: {}, error_count: errorCount, models: {}, duration_seconds: null,
      tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
    },
    turns,
  });

  it('the segmented control renders a labeled radiogroup with four modes + an error badge', async () => {
    const { container } = await renderWithOutline(
      [makeItem({ uuid: 'h1' })],
      baseOutline([oTurn({ uuid: 'h1', kind: 'human' })], 3),
    );
    const seg = container.querySelector('[role="radiogroup"][aria-label="Focus mode"]')!;
    expect(seg).not.toBeNull();
    const radios = seg.querySelectorAll('[role="radio"]');
    expect(radios).toHaveLength(4);
    // All is active by default.
    expect(seg.querySelector('[aria-checked="true"]')!.textContent).toContain('All');
    // Errors carries the count badge.
    expect(container.querySelector('.conv-focus-seg-badge')!.textContent).toBe('3');
  });

  it('v cycles the focus mode all → chat → prompts → errors → all', async () => {
    await renderWithOutline([makeItem({ uuid: 'h1' })], baseOutline([oTurn({ uuid: 'h1', kind: 'human' })]));
    expect(getState().convFocusMode).toBe('all');
    press('v'); expect(getState().convFocusMode).toBe('chat');
    press('v'); expect(getState().convFocusMode).toBe('prompts');
    press('v'); expect(getState().convFocusMode).toBe('errors');
    press('v'); expect(getState().convFocusMode).toBe('all');
  });

  it('prompts mode hides non-human turns behind a hidden-run marker', async () => {
    const { container } = await renderWithOutline(
      [
        makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
        makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0,
          blocks: [{ kind: 'tool_call', name: 'Read', input_summary: '{}', preview: '/a',
            tool_use_id: 't', result: { text: 'ok', truncated: false, is_error: false } }] } as never),
        makeItem({ uuid: 'h2', kind: 'human', text: 'bye' }),
      ],
      baseOutline([
        oTurn({ uuid: 'h1', kind: 'human' }),
        oTurn({ uuid: 'a1', kind: 'assistant' }),
        oTurn({ uuid: 'h2', kind: 'human' }),
      ]),
    );
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    await waitFor(() => expect(container.querySelector('.conv-hidden-run')).not.toBeNull());
    const marker = container.querySelector('.conv-hidden-run')!;
    expect(marker.textContent).toContain('1 hidden');
    // The marker carries data-conv-marker so j/k never land on it.
    expect((marker as HTMLElement).dataset.convMarker).toBe('');
  });

  it('clicking a hidden-run marker resets to all and jumps to the first hidden turn', async () => {
    const { container } = await renderWithOutline(
      [
        makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
        makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0 } as never),
      ],
      baseOutline([oTurn({ uuid: 'h1', kind: 'human' }), oTurn({ uuid: 'a1', kind: 'assistant' })]),
    );
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    const marker = await waitFor(() => container.querySelector('.conv-hidden-run')!);
    fireEvent.click(marker);
    expect(getState().convFocusMode).toBe('all');
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1' });
  });

  it('switching to a mode that hides the focused turn remaps focus to the nearest visible turn, and j/k skip the marker', async () => {
    const { thread } = await renderWithOutline(
      [
        makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
        makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0 } as never),
        makeItem({ uuid: 'h2', kind: 'human', text: 'bye' }),
      ],
      baseOutline([
        oTurn({ uuid: 'h1', kind: 'human' }),
        oTurn({ uuid: 'a1', kind: 'assistant' }),
        oTurn({ uuid: 'h2', kind: 'human' }),
      ]),
    );
    // Focus the assistant turn (index 1).
    press('j');
    expect(thread.children[1]).toHaveClass('conv-item--focused');
    // Switch to prompts: a1 is hidden (a hidden_run marker takes its slot). The
    // remap must move focus onto a real, visible human turn — never the marker.
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    await waitFor(() => expect(thread.querySelector('.conv-hidden-run')).not.toBeNull());
    const focused = thread.querySelector('.conv-item--focused')!;
    expect(focused.classList.contains('conv-hidden-run')).toBe(false);
    expect(focused.getAttribute('data-uuid')).toMatch(/h[12]/);
    // j/k still navigate and never settle on the marker.
    press('j');
    expect(thread.querySelector('.conv-item--focused')!.classList.contains('conv-hidden-run')).toBe(false);
    press('k');
    expect(thread.querySelector('.conv-item--focused')!.classList.contains('conv-hidden-run')).toBe(false);
  });

  // Regression (cross-branch P2): the remap target must resolve in RENDERED-NODE
  // space (`nodes` = what the thread actually renders, time markers AND
  // hidden_run markers included), NOT the marker-less `visible` space. The
  // focused cursor (`focusedIndex`) indexes thread.children = nodes-space, so
  // both the prev-list it reads AND the target it computes must live in
  // nodes-space too. When time markers precede the focused turn in the PRIOR
  // render, the old visible-space `prevVisibleRef[focusedIndex]` reads the wrong
  // slot (or undefined → the remap bails and leaves focusedIndex dangling past
  // the new child count → focus blanks entirely).
  it('remaps focus in rendered-node space when time markers precede the target', async () => {
    // h1 @14:00 (human), a1 @14:20 (tool-only assistant → hidden in prompts),
    // h2 @14:40 (human). ≥10-min gaps mean a time marker precedes BOTH a1 and h2.
    //   ALL nodes:    [h1, marker, a1, marker, h2]            (h2 at index 4)
    //   ALL visible:  [h1, a1, h2]                            (h2 at index 2)
    //   PROMPTS nodes:[h1, hidden_run, marker(h1→h2 40min), h2] (h2 at index 3)
    // The cursor on h2 is nodes-index 4; a visible-space prev list (length 3)
    // has no [4], so the buggy remap bails and focus is lost on the switch.
    const { thread, container } = await renderWithOutline(
      [
        makeItem({ uuid: 'h1', kind: 'human', text: 'hi', ts: '2026-06-12T14:00:00Z' } as never),
        makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0,
          ts: '2026-06-12T14:20:00Z',
          blocks: [{ kind: 'tool_call', name: 'Read', input_summary: '{}', preview: '/a',
            tool_use_id: 't', result: { text: 'ok', truncated: false, is_error: false } }] } as never),
        makeItem({ uuid: 'h2', kind: 'human', text: 'bye', ts: '2026-06-12T14:40:00Z' } as never),
      ],
      baseOutline([
        oTurn({ uuid: 'h1', kind: 'human' }),
        oTurn({ uuid: 'a1', kind: 'assistant' }),
        oTurn({ uuid: 'h2', kind: 'human' }),
      ]),
    );
    // ALL mode renders a time marker before a1 AND before h2.
    await waitFor(() => expect(container.querySelectorAll('.conv-time-marker')).toHaveLength(2));
    // Focus h2 (the later turn). stepFocus skips markers: h1→a1→(skip)→h2.
    press('j'); // a1
    press('j'); // h2 (marker skipped)
    expect(thread.querySelector('.conv-item--focused')!.getAttribute('data-uuid')).toBe('h2');
    // Switch to prompts: a1 collapses into a hidden_run; the 40-min h1→h2 gap
    // inserts a time marker BEFORE h2. The remap must still land focus on h2 —
    // resolved in nodes-space — never a marker, never blank.
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    await waitFor(() => expect(thread.querySelector('.conv-hidden-run')).not.toBeNull());
    const focused = thread.querySelector('.conv-item--focused');
    expect(focused).not.toBeNull();
    expect((focused as HTMLElement).dataset.convMarker).toBeUndefined();
    expect(focused!.classList.contains('conv-time-marker')).toBe(false);
    expect(focused!.classList.contains('conv-hidden-run')).toBe(false);
    expect(focused!.getAttribute('data-uuid')).toBe('h2');
  });
});

describe('ConversationReader jump-to-next keys (#177 S5 §4)', () => {
  async function renderWithOutline(items: ConversationItem[], outline: ConversationOutline) {
    mockFetchOnce(detail(items));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-thread')).not.toBeNull());
    return utils;
  }
  const press = (key: string) => fireEvent.keyDown(document, { key });

  const outline: ConversationOutline = {
    session_id: 's',
    stats: {
      turns: { total: 4, human: 2, assistant: 2, tool_result: 0, meta: 0 },
      tool_counts: {}, error_count: 1, models: {}, duration_seconds: null,
      tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
    },
    turns: [
      oTurn({ uuid: 'h1', kind: 'human' }),
      oTurn({ uuid: 'a1', kind: 'assistant', tools: [{ name: 'Bash', is_error: true }] }),
      oTurn({ uuid: 'h2', kind: 'human' }),
      oTurn({ uuid: 'a2', kind: 'assistant', tools: [{ name: 'ExitPlanMode', is_error: false }] }),
    ],
  };
  const items = [
    makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
    makeItem({ uuid: 'a1', kind: 'assistant', text: 'oops', model: 'm', cost_usd: 0 } as never),
    makeItem({ uuid: 'h2', kind: 'human', text: 'bye' }),
    makeItem({ uuid: 'a2', kind: 'assistant', text: 'plan', model: 'm', cost_usd: 0 } as never),
  ];

  // The cursor resolves from convCurrentTurnUuid first, else the focused child's
  // data-uuid (focus starts on the first turn h1, index 0), else -1. Tests pin
  // the scroll-sync cursor where the jump origin matters.
  it('e jumps to the next error turn (cursor before the start)', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' }); });
    press('e'); // first error strictly after h1 (idx0) → a1 (idx1)
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1' });
  });

  it('u jumps to the next prompt after the cursor', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' }); });
    press('u'); // next human after h1 (idx0) → h2 (idx2)
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'h2' });
  });

  it('U jumps to the previous prompt relative to the scroll-sync cursor', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h2' }); });
    press('U'); // previous prompt before h2 (idx2) → h1 (idx0)
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'h1' });
  });

  it('p jumps to the next plan/question turn', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' }); });
    press('p');
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a2' });
  });

  it('a no-op jump (no target ahead) leaves the jump untouched', async () => {
    await renderWithOutline(items, outline);
    // Park the cursor past the only error (a1) so `e` forward finds nothing.
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'a2' }); });
    press('e');
    expect(getState().conversationJump).toBeNull();
  });
});

// #177 S5 §5 — the store reducer no longer blanket-resets focus mode on a
// same-session OPEN_CONVERSATION, so jumpNext's precise nodeVisible check is
// the sole authority for resetting to `all`. It must reset ONLY when the jump
// target is hidden by the current mode (e.g. an error target in Prompts mode),
// and leave the mode untouched when the target is already visible (e.g. an
// error target in Errors mode). This is the behavior the blanket reset masked.
describe('ConversationReader jump-to-next focus-mode reset (#177 S5 §5)', () => {
  async function renderWithOutline(items: ConversationItem[], outline: ConversationOutline) {
    mockFetchOnce(detail(items));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-thread')).not.toBeNull());
    return utils;
  }
  const press = (key: string) => fireEvent.keyDown(document, { key });

  const errBlock = {
    kind: 'tool_call', name: 'Bash', input_summary: '{}', preview: 'x',
    tool_use_id: 'te', result: { text: 'boom', truncated: false, is_error: true },
  };
  const outline: ConversationOutline = {
    session_id: 's',
    stats: {
      turns: { total: 3, human: 1, assistant: 2, tool_result: 0, meta: 0 },
      tool_counts: {}, error_count: 1, models: {}, duration_seconds: null,
      tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
    },
    turns: [
      oTurn({ uuid: 'h1', kind: 'human' }),
      oTurn({ uuid: 'a1', kind: 'assistant', tools: [{ name: 'Bash', is_error: true }] }),
      oTurn({ uuid: 'h2', kind: 'human' }),
    ],
  };
  const items = [
    makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
    makeItem({ uuid: 'a1', kind: 'assistant', text: 'oops', model: 'm', cost_usd: 0,
      blocks: [errBlock] } as never),
    makeItem({ uuid: 'h2', kind: 'human', text: 'bye' }),
  ];

  it('Errors mode: e-jump to a VISIBLE error target does NOT reset the mode', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'errors' }); });
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' }); });
    press('e'); // → a1, which IS visible in errors mode → no reset
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1' });
    expect(getState().convFocusMode).toBe('errors');
  });

  it('Prompts mode: e-jump to an error target (hidden) DOES reset to all', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' }); });
    press('e'); // → a1, hidden in prompts mode → reset to all before jumping
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1' });
    expect(getState().convFocusMode).toBe('all');
  });
});

// ---- #177 S5 §6 — inter-turn time markers in the reader -------------------
describe('ConversationReader time markers (#177 S5 §6)', () => {
  async function renderInConversations(items: ConversationItem[], outline?: ConversationOutline) {
    mockFetchOnce(detail(items));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-thread')).not.toBeNull());
    return utils;
  }

  it('inserts one gap marker between two turns 42 minutes apart', async () => {
    const { container } = await renderInConversations([
      makeItem({ uuid: 'h1', ts: '2026-06-12T14:00:00Z' } as never),
      makeItem({ uuid: 'h2', ts: '2026-06-12T14:42:00Z' } as never),
    ]);
    await waitFor(() => expect(container.querySelector('.conv-time-marker')).not.toBeNull());
    const markers = container.querySelectorAll('.conv-time-marker');
    expect(markers).toHaveLength(1);
    expect(markers[0].textContent).toContain('42 min later');
    // role="separator" + data-conv-marker → not a keyboard stop.
    expect(markers[0].getAttribute('role')).toBe('separator');
    expect((markers[0] as HTMLElement).dataset.convMarker).toBe('');
  });

  it('emits no marker when adjacent turns are under 10 minutes apart', async () => {
    const { container } = await renderInConversations([
      makeItem({ uuid: 'h1', ts: '2026-06-12T14:00:00Z' } as never),
      makeItem({ uuid: 'h2', ts: '2026-06-12T14:05:00Z' } as never),
    ]);
    // Let the render settle, then assert no marker.
    await waitFor(() => expect(container.querySelector('[data-uuid="h2"]')).not.toBeNull());
    expect(container.querySelector('.conv-time-marker')).toBeNull();
  });

  it('recomputes markers over the visible sequence when the focus mode hides the middle turn', async () => {
    // h1 @14:00, a1 (tool-only assistant, hidden in prompts) @14:05, h2 @14:50.
    // ALL mode: h1→a1 = 5 min (no marker), a1→h2 = 45 min (one "45 min later").
    // PROMPTS mode: a1 is hidden, so h1→h2 spans 50 min → one "50 min later".
    const outline: ConversationOutline = {
      session_id: 's',
      stats: {
        turns: { total: 3, human: 2, assistant: 1, tool_result: 0, meta: 0 },
        tool_counts: {}, error_count: 0, models: {}, duration_seconds: null,
        tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
      },
      turns: [
        oTurn({ uuid: 'h1', kind: 'human' }),
        oTurn({ uuid: 'a1', kind: 'assistant' }),
        oTurn({ uuid: 'h2', kind: 'human' }),
      ],
    };
    const { container } = await renderInConversations([
      makeItem({ uuid: 'h1', kind: 'human', text: 'hi', ts: '2026-06-12T14:00:00Z' } as never),
      makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0,
        ts: '2026-06-12T14:05:00Z',
        blocks: [{ kind: 'tool_call', name: 'Read', input_summary: '{}', preview: '/a',
          tool_use_id: 't', result: { text: 'ok', truncated: false, is_error: false } }] } as never),
      makeItem({ uuid: 'h2', kind: 'human', text: 'bye', ts: '2026-06-12T14:50:00Z' } as never),
    ], outline);

    // ALL mode: a single "45 min later" marker between a1 and h2.
    await waitFor(() => expect(container.querySelector('.conv-time-marker')).not.toBeNull());
    let markers = container.querySelectorAll('.conv-time-marker');
    expect(markers).toHaveLength(1);
    expect(markers[0].textContent).toContain('45 min later');

    // Switch to prompts: a1 vanishes (hidden_run takes its place); the gap now
    // spans h1→h2 = 50 min, recomputed over the visible sequence.
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    await waitFor(() => {
      const m = container.querySelectorAll('.conv-time-marker');
      return expect(m[0]?.textContent).toContain('50 min later');
    });
    markers = container.querySelectorAll('.conv-time-marker');
    expect(markers).toHaveLength(1);
    expect(markers[0].textContent).toContain('50 min later');
  });
});

// #184 — scroll-sync PRODUCER coverage. The reader registers an
// IntersectionObserver over its rendered turns; on a change it dispatches the
// topmost-visible turn's data-uuid to convCurrentTurnUuid. jsdom never lays out
// and the default IO stub is a no-op, so this drives the producer directly: a
// capturing stub records every observer callback, then the test invokes the one
// the reader observed turns with — feeding synthetic intersecting entries whose
// targets carry data-uuid + a mocked getBoundingClientRect top — and asserts the
// store cursor becomes the topmost uuid.
describe('ConversationReader scroll-sync producer (#177 S5 §3 / #184)', () => {
  // A capturing IntersectionObserver: each instance records its callback and the
  // elements it observed, so a test can replay the callback by hand.
  type CapturedObs = { cb: IntersectionObserverCallback; targets: Element[] };
  let observers: CapturedObs[] = [];
  function installCapturingObserver() {
    class Capturing {
      cb: IntersectionObserverCallback;
      targets: Element[] = [];
      constructor(cb: IntersectionObserverCallback) {
        this.cb = cb;
        observers.push(this as unknown as CapturedObs);
      }
      observe(el: Element): void { this.targets.push(el); }
      unobserve(): void {}
      disconnect(): void {}
      takeRecords(): IntersectionObserverEntry[] { return []; }
    }
    (globalThis as unknown as { IntersectionObserver: typeof Capturing }).IntersectionObserver = Capturing;
  }

  beforeEach(() => { observers = []; installCapturingObserver(); });

  it('dispatches the topmost intersecting turn uuid to the store', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1', ts: '2026-06-12T14:00:00Z' } as never),
      makeItem({ uuid: 'h2', ts: '2026-06-12T14:01:00Z' } as never),
    ]));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h2"]')).not.toBeNull());

    const elH1 = container.querySelector('[data-uuid="h1"]') as HTMLElement;
    const elH2 = container.querySelector('[data-uuid="h2"]') as HTMLElement;
    // h2 sits ABOVE h1 in viewport space (smaller top) — it must win.
    vi.spyOn(elH1, 'getBoundingClientRect').mockReturnValue({ top: 200 } as DOMRect);
    vi.spyOn(elH2, 'getBoundingClientRect').mockReturnValue({ top: 40 } as DOMRect);

    // The scroll-sync observer is the one that observed the rendered turn
    // elements (the lazy-load observer observes the sentinel, not these).
    const obs = observers.find((o) => o.targets.includes(elH1) || o.targets.includes(elH2));
    expect(obs).toBeDefined();

    act(() => {
      obs!.cb(
        [
          { target: elH1, isIntersecting: true } as unknown as IntersectionObserverEntry,
          { target: elH2, isIntersecting: true } as unknown as IntersectionObserverEntry,
        ],
        obs as unknown as IntersectionObserver,
      );
    });
    expect(getState().convCurrentTurnUuid).toBe('h2');

    // When h2 scrolls out (no longer intersecting), the topmost falls back to h1.
    act(() => {
      obs!.cb(
        [{ target: elH2, isIntersecting: false } as unknown as IntersectionObserverEntry],
        obs as unknown as IntersectionObserver,
      );
    });
    expect(getState().convCurrentTurnUuid).toBe('h1');
  });
});

// #177 S6 — reader-level wiring of the in-conversation find bar: find →
// jump → disclosure-expand. Parent-level integration (the modal-integration
// precedent): a mocked /find response whose anchor matched in a tool block,
// the target turn carrying a COLLAPSED <details>; opening find, typing, and
// pressing Enter must (a) dispatch the jump with expand_details, (b) open the
// turn's <details>, (c) flash it with conv-item--jumped.
describe('ConversationReader in-conversation find', () => {
  // A tool_call block renders a CLOSED <details className="conv-chip--tool">.
  function detailWithTool() {
    const assistant: ConversationItem = {
      kind: 'assistant',
      anchor: { session_id: 's', uuid: 'a1', id: 2 },
      member_uuids: ['a1'],
      ts: 't',
      text: '',
      blocks: [
        {
          kind: 'tool_call', name: 'Bash', input_summary: 'rg needle',
          preview: 'rg needle', tool_use_id: 'tu1',
          result: { text: 'found needle', truncated: false, is_error: false },
        },
      ],
      model: 'claude-opus-4',
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      cost_usd: 0.01,
    } as ConversationItem;
    return detail([makeItem({ uuid: 'h1', text: 'opening prompt' }), assistant]);
  }

  function installFindRoutedFetch(findBody: unknown) {
    globalThis.fetch = vi.fn(async (url: string | URL) => {
      const u = String(url);
      const body = u.includes('/find') ? findBody : detailWithTool();
      return { ok: true, status: 200, json: async () => body } as Response;
    }) as unknown as typeof fetch;
  }

  it('typing + Enter jumps to the matched turn, opens its collapsed details, and flashes it', async () => {
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    installFindRoutedFetch({
      anchors: [{ uuid: 'a1', match_kinds: ['tool'] }],
      total: 1, anchors_truncated: false, mode: 'fts', search_depth: 'full',
    });
    installGlobalKeydown();
    // Land on the session so OPEN_CONVERSATION jumps are same-session (find stays open).
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });

    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="a1"]')).not.toBeNull());

    // The matched turn's tool disclosure starts CLOSED.
    const det = container.querySelector('[data-uuid="a1"] details.conv-chip--tool') as HTMLDetailsElement;
    expect(det).not.toBeNull();
    expect(det.open).toBe(false);

    // Open the find bar (the '/' rebind dispatches this; here we drive the store
    // directly to keep the test reader-scoped).
    act(() => { dispatch({ type: 'OPEN_CONV_FIND' }); });
    const input = await waitFor(() => {
      const el = container.querySelector<HTMLInputElement>('.conv-findbar-input');
      expect(el).not.toBeNull();
      return el!;
    });

    // Type a needle → debounced find fetch → one anchor.
    fireEvent.change(input, { target: { value: 'needle' } });
    await waitFor(() => expect(container.querySelector('.conv-findbar-count')!.textContent).toContain('1 / 1'));

    // Enter steps to the (only) anchor and jumps with expand_details (tool match).
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }); });
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1', expand_details: true });

    // The jump effect opens the disclosure, scrolls, and flashes the turn.
    await waitFor(() => expect((container.querySelector('[data-uuid="a1"] details.conv-chip--tool') as HTMLDetailsElement).open).toBe(true));
    expect(scrollSpy).toHaveBeenCalled();
    await waitFor(() => {
      const target = container.querySelector('[data-uuid="a1"]')!;
      expect(target.classList.contains('conv-item--jumped')).toBe(true);
    });
  });
});
