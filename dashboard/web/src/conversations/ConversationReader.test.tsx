import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationReader } from './ConversationReader';
import { _resetForTests, dispatch, getState } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymapForTests,
} from '../store/keymap';
import { installIntersectionObserverStub } from '../test-utils/intersectionObserver';
import type { ConversationItem } from '../types/conversation';

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
});
