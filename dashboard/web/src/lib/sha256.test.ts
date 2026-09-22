import { beforeEach, describe, expect, it, vi } from 'vitest';
import { sha256Hex } from './sha256';

describe('sha256Hex', () => {
  beforeEach(() => {
    // Plain HTTP LAN pages can expose crypto without the WebCrypto digest API.
    vi.stubGlobal('crypto', { subtle: undefined });
  });

  it.each([
    ['', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
    ['abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
    [
      'The quick brown fox jumps over the lazy dog',
      'd7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592',
    ],
    [
      'abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq',
      '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1',
    ],
  ])('matches the SHA-256 fallback vector for %j', async (input, expected) => {
    await expect(sha256Hex(new TextEncoder().encode(input))).resolves.toBe(expected);
  });

  it('handles binary bytes and a two-block padding boundary', async () => {
    await expect(sha256Hex(Uint8Array.from([0, 1, 2, 3, 4, 5, 127, 128, 255]))).resolves.toBe(
      '39a78da6ab932c756688830b315b7a3726b811d94ea98ff7d4bb4d46cbb6e60a',
    );
    await expect(sha256Hex(new Uint8Array(56).fill(0x61))).resolves.toBe(
      'b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a',
    );
  });

  it('prefers native WebCrypto when its digest API is available', async () => {
    const nativeDigest = new Uint8Array(32).fill(0xab).buffer;
    const digest = vi.fn(async () => nativeDigest);
    vi.stubGlobal('crypto', { subtle: { digest } });

    await expect(sha256Hex(new Uint8Array([1, 2, 3]))).resolves.toBe('ab'.repeat(32));
    expect(digest).toHaveBeenCalledWith('SHA-256', expect.any(ArrayBuffer));
  });

  it('yields to a macrotask and observes abort during fallback hashing', async () => {
    const bytes = new Uint8Array(4 * 1024 * 1024).fill(0x61);
    const controller = new AbortController();
    let timerRan = false;
    const timer = setTimeout(() => {
      timerRan = true;
      controller.abort();
    }, 0);

    try {
      await expect(sha256Hex(bytes, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
    } finally {
      clearTimeout(timer);
    }
    expect(timerRan).toBe(true);
  });

  it('keeps the exact digest across macrotask yields for a multi-MiB payload', async () => {
    const bytes = new Uint8Array(4 * 1024 * 1024).fill(0x61);

    await expect(sha256Hex(bytes)).resolves.toBe(
      '299285fc41a44cdb038b9fdaf494c76ca9d0c866672b2b266c1a0c17dda60a05',
    );
  });

  it('checks abort before invoking native WebCrypto', async () => {
    const controller = new AbortController();
    controller.abort();
    const digest = vi.fn(async () => new Uint8Array(32).buffer);
    vi.stubGlobal('crypto', { subtle: { digest } });

    await expect(sha256Hex(new Uint8Array([1, 2, 3]), controller.signal))
      .rejects.toMatchObject({ name: 'AbortError' });
    expect(digest).not.toHaveBeenCalled();
  });

  it('does not publish a native digest after abort', async () => {
    const controller = new AbortController();
    const digest = vi.fn(async () => {
      controller.abort();
      return new Uint8Array(32).buffer;
    });
    vi.stubGlobal('crypto', { subtle: { digest } });

    await expect(sha256Hex(new Uint8Array([1, 2, 3]), controller.signal))
      .rejects.toMatchObject({ name: 'AbortError' });
    expect(digest).toHaveBeenCalledOnce();
  });
});
