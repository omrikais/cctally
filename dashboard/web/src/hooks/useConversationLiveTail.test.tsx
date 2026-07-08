import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversationLiveTail } from './useConversationLiveTail';

// Minimal EventSource mock (copied from useConversation.test). The lifted
// live-tail hook opens one of these per conversation and fires on
// `ready`/`tail`/`error`.
class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
  closed = false;
  constructor(url: string) { this.url = url; MockEventSource.instances.push(this); }
  addEventListener(name: string, fn: (ev: MessageEvent) => void): void {
    (this.listeners[name] ||= []).push(fn);
  }
  close(): void { this.closed = true; }
  emit(name: string, data: unknown = {}): void {
    (this.listeners[name] || []).forEach((fn) => fn({ data: JSON.stringify(data) } as MessageEvent));
  }
}

// Drive `transcriptsEnabled` (the useSnapshot gate) and `selectLiveTailEnabled`
// (the store gate) deterministically. Mirrors useConversation.test.
let mockTranscripts = true;
let mockLiveTail = true;
vi.mock('./useSnapshot', () => ({
  useSnapshot: () => ({ generated_at: 't0', transcriptsEnabled: mockTranscripts }),
}));
vi.mock('../store/store', async (orig) => ({
  ...(await orig<typeof import('../store/store')>()),
  selectLiveTailEnabled: () => mockLiveTail,
}));

beforeEach(() => {
  mockTranscripts = true;
  mockLiveTail = true;
  (globalThis as unknown as { EventSource: typeof MockEventSource }).EventSource = MockEventSource;
  MockEventSource.instances = [];
});
afterEach(() => vi.restoreAllMocks());

function es() {
  return MockEventSource.instances.find((e) => e.url.includes('/api/conversation/sid/events'));
}

describe('useConversationLiveTail', () => {
  it('live flips true only on ready; growthNonce bumps on ready and tail', async () => {
    const { result } = renderHook(() => useConversationLiveTail('sid'));
    await waitFor(() => expect(es()).toBeTruthy());
    // Socket open ≠ live: `live` stays false until the server sends `ready`.
    expect(result.current.live).toBe(false);
    expect(result.current.growthNonce).toBe(0);

    await act(async () => { es()!.emit('ready'); await Promise.resolve(); });
    expect(result.current.live).toBe(true);
    expect(result.current.growthNonce).toBe(1);

    await act(async () => { es()!.emit('tail', { sessionId: 'sid' }); await Promise.resolve(); });
    expect(result.current.live).toBe(true);
    expect(result.current.growthNonce).toBe(2);

    await act(async () => { es()!.emit('error'); await Promise.resolve(); });
    expect(result.current.live).toBe(false);
  });

  it('never subscribes when transcripts are disabled', async () => {
    mockTranscripts = false;
    const { result } = renderHook(() => useConversationLiveTail('sid'));
    await waitFor(() => expect(result.current.live).toBe(false));
    expect(MockEventSource.instances.every((e) => !e.url.includes('/events'))).toBe(true);
  });

  it('never subscribes when live-tail is disabled', async () => {
    mockLiveTail = false;
    const { result } = renderHook(() => useConversationLiveTail('sid'));
    await waitFor(() => expect(result.current.live).toBe(false));
    expect(MockEventSource.instances.every((e) => !e.url.includes('/events'))).toBe(true);
  });

  it('never subscribes when sessionId is null', async () => {
    renderHook(() => useConversationLiveTail(null));
    expect(MockEventSource.instances.every((e) => !e.url.includes('/events'))).toBe(true);
  });

  it('closes the EventSource and resets live on a session change', async () => {
    const { result, rerender } = renderHook(({ id }) => useConversationLiveTail(id),
      { initialProps: { id: 'sid' as string | null } });
    await waitFor(() => expect(es()).toBeTruthy());
    await act(async () => { es()!.emit('ready'); await Promise.resolve(); });
    expect(result.current.live).toBe(true);
    const first = es()!;
    act(() => { rerender({ id: 'sid2' }); });
    expect(first.closed).toBe(true);
    expect(result.current.live).toBe(false);   // reset on the new subscription
  });
});
