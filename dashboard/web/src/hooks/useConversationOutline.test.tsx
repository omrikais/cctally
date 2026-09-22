import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createHash } from 'node:crypto';
import { useConversationOutline } from './useConversationOutline';

// Mock the snapshot store so we can drive `generated_at` (the SSE-tick signal
// the refetch effect keys on) deterministically. Mirrors useConversation.test.
// #300 — also expose `data_version` (the real change signal). `undefined` (the
// default) makes the hook fall back to `generated_at`, so the pre-#300 tests
// below keep exercising the every-tick fallback path unchanged.
let mockGeneratedAt = 't0';
let mockDataVersion: string | undefined = undefined;
vi.mock('./useSnapshot', () => ({
  useSnapshot: () => ({ generated_at: mockGeneratedAt, data_version: mockDataVersion }),
}));

function outline(session_id: string, over: Record<string, unknown> = {}) {
  return {
    session_id,
    stats: {
      turns: { total: 0, human: 0, assistant: 0, tool_result: 0, meta: 0 },
      tool_counts: {}, error_count: 0, models: {}, duration_seconds: null,
      tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
    },
    turns: [],
    ...over,
  };
}

function mockOnce(body: unknown, status = 200) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: status < 400, status, json: async () => body } as Response);
}

function sha256(bytes: Uint8Array) {
  return createHash('sha256').update(bytes).digest('hex');
}
beforeEach(() => { globalThis.fetch = vi.fn(); mockGeneratedAt = 't0'; mockDataVersion = undefined; });
afterEach(() => vi.restoreAllMocks());

function bumpTick(rerender: () => void, tag: string) {
  mockGeneratedAt = tag;
  rerender();
}

describe('useConversationOutline', () => {
  it('hydrates a valid progressive outline without WebCrypto digest support (#833)', async () => {
    vi.stubGlobal('crypto', { subtle: undefined });
    const full = outline('s', { stats: { ...outline('s').stats, cost_usd: 2 } });
    const bytes = new TextEncoder().encode(JSON.stringify(full));
    const encoded = btoa(Array.from(bytes, (byte) => String.fromCharCode(byte)).join(''));
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((rawUrl: string) => {
      const url = String(rawUrl);
      const body = url.includes('/outline-transfer/')
        ? {
            offset: 0, next_offset: bytes.length, total: bytes.length,
            sha256: sha256(bytes), done: true, chunk: encoded,
          }
        : { progressive: 1, transfer: { token: 'opaque', chunk_size: 196608 } };
      return Promise.resolve({ ok: true, status: 200, json: async () => body } as Response);
    });

    const { result } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(2));
    expect(result.current.error).toBeNull();
  });

  it('rejects a corrupt progressive outline without WebCrypto digest support (#833)', async () => {
    vi.stubGlobal('crypto', { subtle: undefined });
    const full = outline('s');
    const bytes = new TextEncoder().encode(JSON.stringify(full));
    const tampered = bytes.slice();
    const costNeedle = new TextEncoder().encode('"cost_usd":0');
    let costOffset = -1;
    for (let i = 0; i <= tampered.length - costNeedle.length; i += 1) {
      if (costNeedle.every((byte, j) => tampered[i + j] === byte)) {
        costOffset = i + costNeedle.length - 1;
        break;
      }
    }
    expect(costOffset).toBeGreaterThanOrEqual(0);
    tampered[costOffset] = '1'.charCodeAt(0);
    const encoded = btoa(Array.from(tampered, (byte) => String.fromCharCode(byte)).join(''));
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((rawUrl: string) => {
      const url = String(rawUrl);
      const body = url.includes('/outline-transfer/')
        ? {
            offset: 0, next_offset: tampered.length, total: tampered.length,
            sha256: sha256(bytes), done: true, chunk: encoded,
          }
        : { progressive: 1, transfer: { token: 'opaque', chunk_size: 196608 } };
      return Promise.resolve({ ok: true, status: 200, json: async () => body } as Response);
    });

    const { result } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.error).toBe("Couldn't load the outline."));
    expect(result.current.outline).toBeNull();
  });

  it('does not publish a progressive outline when the session switches during fallback hashing (#833)', async () => {
    vi.stubGlobal('crypto', { subtle: undefined });
    const full = outline('s', { filler: 'a'.repeat(2 * 1024 * 1024) });
    const bytes = new TextEncoder().encode(JSON.stringify(full));
    const encoded = btoa(Array.from(bytes, (byte) => String.fromCharCode(byte)).join(''));
    let timerRan = false;
    let outlinePublishedBeforeAbort = false;
    let readOutline: () => ReturnType<typeof useConversationOutline>['outline'] = () => null;

    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      (rawUrl: RequestInfo | URL, init?: RequestInit) => {
        const url = String(rawUrl);
        if (init?.method === 'DELETE') {
          return Promise.resolve({ ok: true, status: 204 } as Response);
        }
        if (url.includes('/outline-transfer/')) {
          setTimeout(() => {
            timerRan = true;
            outlinePublishedBeforeAbort = readOutline() !== null;
            act(() => rerender({ sid: null }));
          }, 0);
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({
              offset: 0, next_offset: bytes.length, total: bytes.length,
              sha256: sha256(bytes), done: true, chunk: encoded,
            }),
          } as Response);
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({
            progressive: 1,
            transfer: { token: 'opaque', chunk_size: 196608 },
          }),
        } as Response);
      },
    );

    const { result, rerender, unmount } = renderHook(
      ({ sid }) => useConversationOutline(sid),
      { initialProps: { sid: 's' as string | null } },
    );
    readOutline = () => result.current.outline;
    await waitFor(() => expect(timerRan).toBe(true));
    expect(outlinePublishedBeforeAbort).toBe(false);
    expect(result.current.outline).toBeNull();
    unmount();
  });

  it('keeps the outline loading while hydrating the exact bounded transfer (#682)', async () => {
    const full = outline('s', { stats: { ...outline('s').stats, cost_usd: 2 } });
    const bytes = new TextEncoder().encode(JSON.stringify(full));
    const encoded = btoa(Array.from(bytes, (byte) => String.fromCharCode(byte)).join(''));
    let resolveChunk!: () => void;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((rawUrl: string) => {
      const url = String(rawUrl);
      if (url.includes('/outline-transfer/')) {
        return new Promise((resolve) => {
          resolveChunk = () => resolve({
            ok: true,
            status: 200,
            json: async () => ({
              offset: 0, next_offset: bytes.length, total: bytes.length,
              sha256: sha256(bytes), done: true, chunk: encoded,
            }),
          } as Response);
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          progressive: 1,
          transfer: { token: 'opaque', chunk_size: 196608 },
        }),
      } as Response);
    });

    const { result } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(resolveChunk).toBeTypeOf('function'));
    expect(result.current.outline).toBeNull();
    expect(result.current.loading).toBe(true);
    await act(async () => { resolveChunk(); await Promise.resolve(); });
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(2));
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map(([url]) => String(url))).toEqual([
      expect.stringContaining('/outline?progressive=1'),
      '/api/conversation/outline-transfer/opaque?offset=0',
    ]);
  });

  it('rejects a transfer whose bytes do not match the advertised SHA-256 (#682)', async () => {
    const full = outline('s');
    const bytes = new TextEncoder().encode(JSON.stringify(full));
    const tampered = bytes.slice();
    const costNeedle = new TextEncoder().encode('"cost_usd":0');
    let costOffset = -1;
    for (let i = 0; i <= tampered.length - costNeedle.length; i += 1) {
      if (costNeedle.every((byte, j) => tampered[i + j] === byte)) {
        costOffset = i + costNeedle.length - 1;
        break;
      }
    }
    expect(costOffset).toBeGreaterThanOrEqual(0);
    tampered[costOffset] = '1'.charCodeAt(0);
    const encoded = btoa(Array.from(tampered, (byte) => String.fromCharCode(byte)).join(''));
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((rawUrl: string) => {
      const url = String(rawUrl);
      const body = url.includes('/outline-transfer/')
        ? {
            offset: 0, next_offset: tampered.length, total: tampered.length,
            sha256: sha256(bytes), done: true, chunk: encoded,
          }
        : { progressive: 1, transfer: { token: 'opaque', chunk_size: 196608 } };
      return Promise.resolve({ ok: true, status: 200, json: async () => body } as Response);
    });

    const { result } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.error).toBe("Couldn't load the outline."));
    expect(result.current.outline).toBeNull();
  });

  it('rejects a changed digest even when every chunk repeats it (#682)', async () => {
    const full = outline('s');
    const bytes = new TextEncoder().encode(JSON.stringify(full));
    const encoded = btoa(Array.from(bytes, (byte) => String.fromCharCode(byte)).join(''));
    const digest = sha256(bytes);
    const wrongDigest = `${digest.slice(0, -1)}${digest.endsWith('0') ? '1' : '0'}`;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((rawUrl: string) => {
      const url = String(rawUrl);
      const body = url.includes('/outline-transfer/')
        ? {
            offset: 0, next_offset: bytes.length, total: bytes.length,
            sha256: wrongDigest, done: true, chunk: encoded,
          }
        : { progressive: 1, transfer: { token: 'opaque', chunk_size: 196608 } };
      return Promise.resolve({ ok: true, status: 200, json: async () => body } as Response);
    });

    const { result } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.error).toBe("Couldn't load the outline."));
    expect(result.current.outline).toBeNull();
  });

  it('fetches the outline on a session id', async () => {
    mockOnce(outline('s'));
    const { result } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s'));
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('/api/conversation/s/outline');
    expect(result.current.error).toBeNull();
  });

  it('treats a progressive no-transcript answer as absence without requesting a transfer', async () => {
    mockOnce({ status: 'not_found' });
    const { result } = renderHook(() => useConversationOutline('cost-only'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.outline).toBeNull();
    expect(result.current.error).toBeNull();
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it('recognizes a legacy missing-transcript 404 but does not conceal a server fault', async () => {
    mockOnce({}, 404);
    const { result, rerender } = renderHook(({ sid }) => useConversationOutline(sid), {
      initialProps: { sid: 'old-server' },
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
    mockOnce({}, 500);
    rerender({ sid: 'broken-server' });
    await waitFor(() => expect(result.current.error).toBe("Couldn't load the outline."));
  });

  it.each([
    ['bare', 'maintenance', 'busy with maintenance'],
    ['qualified', 'schema_behind', 'behind this version'],
  ])('classifies a %s 200 degraded outline as %s without parsing an empty outline', async (kind, reason, message) => {
    mockOnce({ status: 'degraded', degraded_reason: reason });
    const ref = kind === 'qualified' ? { source: 'codex' as const, key: 'v1.codex-1' } : 'bare-1';
    const { result } = renderHook(() => useConversationOutline(ref));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.outline).toBeNull();
    expect(result.current.degraded).toMatchObject({ reason, message: expect.stringContaining(message) });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it('qualified Codex outline loads totals without a redundant detail request (#477)', async () => {
    const ref = { source: 'codex' as const, key: 'v1.codex-heavy' };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((rawUrl: string) => {
      const url = String(rawUrl);
      const body = url.includes('/outline')
        ? {
            status: 'ok', conversation_key: ref.key, turns: [], files: [], children: [],
            stats: {
              items: 0, kinds: {}, cost_usd: 12.5,
              tokens: { source: 'codex', input: 100, output: 20, cached_input: 40, reasoning_output: 5 },
            },
          }
        : {
            status: 'ok', conversation_key: ref.key, items: [], children: [], parent: null,
            page: { total: 0, returned: 0, before: null, after: null, has_before: false, has_after: false },
            total_cost_usd: 12.5,
            tokens: { source: 'codex', input: 100, output: 20, cached_input: 40, reasoning_output: 5 },
          };
      return Promise.resolve({ ok: true, status: 200, json: async () => body } as Response);
    });

    const { result } = renderHook(() => useConversationOutline(ref));
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(12.5));

    const urls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map(([url]) => String(url));
    expect(urls).toEqual([expect.stringContaining('/outline')]);
    expect(urls.some((url) => url.includes('limit=1'))).toBe(false);
    expect(result.current.outline?.stats.tokens).toMatchObject({
      source: 'codex', input: 100, output: 20, cached_input: 40, reasoning_output: 5,
    });
  });

  it('resets to null when the session switches', async () => {
    mockOnce(outline('s1'));
    const { result, rerender } = renderHook(({ sid }) => useConversationOutline(sid), { initialProps: { sid: 's1' as string | null } });
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s1'));

    // Defer s2's fetch so we can observe the synchronous reset to null.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(() => {}));
    act(() => { rerender({ sid: 's2' }); });
    expect(result.current.outline).toBeNull();
  });

  it('null session id → no fetch, outline null, not loading', async () => {
    const { result } = renderHook(() => useConversationOutline(null));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.outline).toBeNull();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('refetches on a generated_at change', async () => {
    mockOnce(outline('s', { stats: { ...outline('s').stats, cost_usd: 1 } }));
    const { result, rerender } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(1));

    // Next tick: a fresh outline (cost bumped).
    mockOnce(outline('s', { stats: { ...outline('s').stats, cost_usd: 2 } }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(2));
  });

  it('revalidateOnTick:false suppresses the tick-driven refetch (#227)', async () => {
    mockOnce(outline('s'));
    const { result, rerender } = renderHook(() => useConversationOutline('s', { revalidateOnTick: false }));
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s'));
    // Initial load fired once.
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);

    // A generated_at bump must NOT trigger a refetch when opted out.
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await act(async () => { bumpTick(rerender, 't2'); await Promise.resolve(); });
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);
    expect(result.current.outline?.session_id).toBe('s');
  });

  it('coalesces a tick that lands mid-fetch into exactly one trailing refetch', async () => {
    let resolveSecond!: (body: unknown) => void;
    let fetchCount = 0;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() => {
      fetchCount += 1;
      if (fetchCount === 1) {
        // Initial open resolves immediately.
        return Promise.resolve({ ok: true, status: 200, json: async () => outline('s') } as Response);
      }
      if (fetchCount === 2) {
        // First tick-driven refetch is held pending.
        return new Promise((resolve) => {
          resolveSecond = (body: unknown) => resolve({ ok: true, status: 200, json: async () => body } as Response);
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => outline('s') } as Response);
    });

    const { result, rerender } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s'));

    // Tick 1: kicks off refetch #2 (held pending).
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(fetchCount).toBe(2));

    // Ticks 2 and 3 land while #2 is still in flight: coalesced, no new fetch.
    await act(async () => { bumpTick(rerender, 't2'); await Promise.resolve(); });
    await act(async () => { bumpTick(rerender, 't3'); await Promise.resolve(); });
    expect(fetchCount).toBe(2);

    // Resolve #2; the finally replays exactly ONE coalesced refetch (#3).
    await act(async () => { resolveSecond(outline('s')); for (let i = 0; i < 6; i++) await Promise.resolve(); });
    await waitFor(() => expect(fetchCount).toBe(3));
    expect(fetchCount).toBe(3);
  });

  it('a fetch error degrades to {outline: null, error} without throwing', async () => {
    mockOnce({}, 500);
    const { result } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.outline).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('with live=true, a generated_at tick does NOT refetch (#278)', async () => {
    mockOnce(outline('s'));
    const { result, rerender } = renderHook(
      ({ live }) => useConversationOutline('s', { live }),
      { initialProps: { live: true } });
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s'));
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);
    // Two ticks while live-tailing: no refetch (growth arrives via growthNonce).
    mockGeneratedAt = 't1';
    await act(async () => { rerender({ live: true }); await Promise.resolve(); });
    mockGeneratedAt = 't2';
    await act(async () => { rerender({ live: true }); await Promise.resolve(); });
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);
  });

  it('a growthNonce bump refetches even while live (#278)', async () => {
    mockOnce(outline('s', { stats: { ...outline('s').stats, cost_usd: 1 } }));
    const { result, rerender } = renderHook(
      ({ nonce, live }) => useConversationOutline('s', { growthNonce: nonce, live }),
      { initialProps: { nonce: 0, live: true } });
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(1));
    mockOnce(outline('s', { stats: { ...outline('s').stats, cost_usd: 2 } }));
    await act(async () => { rerender({ nonce: 1, live: true }); await Promise.resolve(); });
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(2));
  });

  it('with live=false, a generated_at tick still refetches (fallback)', async () => {
    mockOnce(outline('s', { stats: { ...outline('s').stats, cost_usd: 1 } }));
    const { result, rerender } = renderHook(
      ({ live }) => useConversationOutline('s', { live }),
      { initialProps: { live: false } });
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(1));
    mockOnce(outline('s', { stats: { ...outline('s').stats, cost_usd: 2 } }));
    mockGeneratedAt = 't1';
    await act(async () => { rerender({ live: false }); await Promise.resolve(); });
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(2));
  });

  it('drops a stale-session response when the session switched mid-fetch', async () => {
    // s1's fetch is held pending; we switch to s2 (resolves immediately) and
    // only THEN resolve s1. The stale s1 body must not land under s2.
    let resolveS1!: (body: unknown) => void;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/api/conversation/s1/outline')) {
        return new Promise((resolve) => {
          resolveS1 = (body: unknown) => resolve({ ok: true, status: 200, json: async () => body } as Response);
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => outline('s2') } as Response);
    });

    const { result, rerender } = renderHook(({ sid }) => useConversationOutline(sid), { initialProps: { sid: 's1' as string | null } });
    // s1 fetch is pending; switch to s2.
    rerender({ sid: 's2' });
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s2'));

    // Now resolve the stale s1 fetch — it must be dropped.
    await act(async () => { resolveS1(outline('s1')); for (let i = 0; i < 4; i++) await Promise.resolve(); });
    expect(result.current.outline?.session_id).toBe('s2');
  });

  it('keeps progressive request ownership when the session switches mid-transfer (#682)', async () => {
    const oldBytes = new TextEncoder().encode(JSON.stringify(outline('s1')));
    const oldSplit = Math.floor(oldBytes.length / 2);
    const newBytes = new TextEncoder().encode(JSON.stringify(outline('s2')));
    const encode = (bytes: Uint8Array) => btoa(
      Array.from(bytes, (byte) => String.fromCharCode(byte)).join(''));
    let resolveOldFirst!: () => void;
    let oldSignal: AbortSignal | undefined;
    let resolveNewChunk!: () => void;
    let oldContinuationCount = 0;
    let oldCancellationCount = 0;
    let newOutlineCount = 0;

    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      (rawUrl: RequestInfo | URL, init?: RequestInit) => {
        const url = String(rawUrl);
        if (url.includes('/api/conversation/s1/outline')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({
              progressive: 1,
              transfer: { token: 'old', chunk_size: oldSplit },
            }),
          } as Response);
        }
        if (url === '/api/conversation/outline-transfer/old?offset=0') {
          oldSignal = init?.signal ?? undefined;
          return new Promise((resolve) => {
            resolveOldFirst = () => resolve({
              ok: true,
              status: 200,
              json: async () => ({
                offset: 0,
                next_offset: oldSplit,
                total: oldBytes.length,
                sha256: sha256(oldBytes),
                done: false,
                chunk: encode(oldBytes.slice(0, oldSplit)),
              }),
            } as Response);
          });
        }
        if (url === `/api/conversation/outline-transfer/old?offset=${oldSplit}`) {
          oldContinuationCount += 1;
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({
              offset: oldSplit,
              next_offset: oldBytes.length,
              total: oldBytes.length,
              sha256: sha256(oldBytes),
              done: true,
              chunk: encode(oldBytes.slice(oldSplit)),
            }),
          } as Response);
        }
        if (url === '/api/conversation/outline-transfer/old'
            && init?.method === 'DELETE') {
          oldCancellationCount += 1;
          expect(init.keepalive).toBe(true);
          return Promise.resolve({ ok: true, status: 204 } as Response);
        }
        if (url.includes('/api/conversation/s2/outline')) {
          newOutlineCount += 1;
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({
              progressive: 1,
              summary: outline('s2'),
              transfer: { token: 'new', chunk_size: newBytes.length },
            }),
          } as Response);
        }
        if (url === '/api/conversation/outline-transfer/new?offset=0') {
          return new Promise((resolve) => {
            resolveNewChunk = () => resolve({
              ok: true,
              status: 200,
              json: async () => ({
                offset: 0,
                next_offset: newBytes.length,
                total: newBytes.length,
                sha256: sha256(newBytes),
                done: true,
                chunk: encode(newBytes),
              }),
            } as Response);
          });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      });

    const { result, rerender, unmount } = renderHook(
      ({ sid }) => useConversationOutline(sid),
      { initialProps: { sid: 's1' as string | null } },
    );
    await waitFor(() => expect(resolveOldFirst).toBeTypeOf('function'));

    rerender({ sid: 's2' });
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s2'));
    await waitFor(() => expect(resolveNewChunk).toBeTypeOf('function'));

    await act(async () => {
      resolveOldFirst();
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
    await act(async () => {
      mockGeneratedAt = 't1';
      rerender({ sid: 's2' });
      await Promise.resolve();
    });

    expect(oldSignal?.aborted).toBe(true);
    expect(oldContinuationCount).toBe(0);
    expect(oldCancellationCount).toBe(1);
    expect(newOutlineCount).toBe(1);

    unmount();
    resolveNewChunk();
  });

  // #300 — the non-live fallback must gate on the change signal (data_version),
  // not the 5s `generated_at` heartbeat: a finished/static conversation open in
  // the reader fetches its outline once and is not re-GET every tick.
  it('with data_version present, a generated_at-only tick does NOT refetch (#300)', async () => {
    mockDataVersion = 'v1';
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      { ok: true, status: 200, json: async () => outline('s') } as Response);
    const { result, rerender } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s'));
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);
    // generated_at advances twice, data_version stays flat → no refetch.
    await act(async () => { mockGeneratedAt = 't1'; rerender(); await Promise.resolve(); });
    await act(async () => { mockGeneratedAt = 't2'; rerender(); await Promise.resolve(); });
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);
  });

  it('a data_version change refetches even when generated_at is unchanged (#300)', async () => {
    mockDataVersion = 'v1';
    mockOnce(outline('s', { stats: { ...outline('s').stats, cost_usd: 1 } }));
    const { result, rerender } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(1));
    // generated_at is left at 't0'; only the change signal advances → refetch.
    mockOnce(outline('s', { stats: { ...outline('s').stats, cost_usd: 2 } }));
    await act(async () => { mockDataVersion = 'v2'; rerender(); await Promise.resolve(); });
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(2));
  });
});
