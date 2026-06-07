import { render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationReader } from './ConversationReader';
import { _resetForTests, dispatch, getState } from '../store/store';
import type { ConversationItem } from '../types/conversation';

// jsdom lacks IntersectionObserver — install a minimal no-op so the
// lazy-load sentinel effect can mount without throwing.
class IntersectionObserverStub {
  constructor(_cb: IntersectionObserverCallback) {}
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] { return []; }
}

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
  globalThis.fetch = vi.fn();
  (globalThis as unknown as { IntersectionObserver: typeof IntersectionObserverStub }).IntersectionObserver =
    IntersectionObserverStub;
});
afterEach(() => {
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
});
