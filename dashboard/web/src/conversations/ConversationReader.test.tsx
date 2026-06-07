import { render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationReader } from './ConversationReader';
import { _resetForTests, dispatch } from '../store/store';
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
  it('renders the header and grouped items (a sidechain run collapses into one group)', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 's1', is_sidechain: true }),
      makeItem({ uuid: 's2', is_sidechain: true }),
      makeItem({ uuid: 'h2' }),
    ]));
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader-body')).not.toBeNull());

    // Header carries whole-session cost + branch + models.
    expect(container.querySelector('.conv-reader-title')!.textContent).toBe('proj');
    expect(container.querySelector('.conv-reader-meta')!.textContent).toContain('$3.50');
    expect(container.querySelector('.conv-reader-meta')!.textContent).toContain('main');

    // Two top-level human items + one collapsed sidechain group.
    const body = container.querySelector('.conv-reader-body')!;
    expect(body.querySelector('[data-uuid="h1"]')).not.toBeNull();
    expect(body.querySelector('[data-uuid="h2"]')).not.toBeNull();
    const sidechain = body.querySelector('details.conv-sidechain')!;
    expect(sidechain).not.toBeNull();
    expect(sidechain.querySelector('summary')!.textContent).toContain('2 messages');
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
});
