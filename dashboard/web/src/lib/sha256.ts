const ROUND_CONSTANTS = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
  0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
  0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
  0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
  0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
  0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

const INITIAL_HASH = [
  0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
  0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
];

// Keep each fallback hash task bounded while avoiding a timer yield for every
// 64-byte block. This is small enough for responsive input on slower devices,
// while a 32 MiB transfer yields a bounded number of times.
const FALLBACK_BLOCKS_PER_YIELD = 1024;

function rotateRight(value: number, bits: number): number {
  return (value >>> bits) | (value << (32 - bits));
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw signal.reason ?? new DOMException('Aborted', 'AbortError');
}

function yieldToEventLoop(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function sha256Fallback(bytes: Uint8Array, signal?: AbortSignal): Promise<string> {
  throwIfAborted(signal);
  const paddedLength = Math.ceil((bytes.byteLength + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(bytes);
  padded[bytes.byteLength] = 0x80;

  // The outline transfer is bounded at 32 MiB, so this length arithmetic is
  // exact while still writing the full SHA-256 64-bit bit length field.
  const view = new DataView(padded.buffer);
  const bitLength = bytes.byteLength * 8;
  view.setUint32(paddedLength - 8, Math.floor(bitLength / 0x100000000), false);
  view.setUint32(paddedLength - 4, bitLength >>> 0, false);

  const hash = INITIAL_HASH.slice();
  const schedule = new Uint32Array(64);
  for (let offset = 0; offset < paddedLength; offset += 64) {
    throwIfAborted(signal);
    for (let word = 0; word < 16; word += 1) {
      schedule[word] = view.getUint32(offset + word * 4, false);
    }
    for (let word = 16; word < 64; word += 1) {
      const previous15 = schedule[word - 15];
      const previous2 = schedule[word - 2];
      const sigma0 = rotateRight(previous15, 7)
        ^ rotateRight(previous15, 18)
        ^ (previous15 >>> 3);
      const sigma1 = rotateRight(previous2, 17)
        ^ rotateRight(previous2, 19)
        ^ (previous2 >>> 10);
      schedule[word] = (schedule[word - 16] + sigma0 + schedule[word - 7] + sigma1) >>> 0;
    }

    let [a, b, c, d, e, f, g, h] = hash;
    for (let word = 0; word < 64; word += 1) {
      const bigSigma1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choose = (e & f) ^ (~e & g);
      const temp1 = (h + bigSigma1 + choose + ROUND_CONSTANTS[word] + schedule[word]) >>> 0;
      const bigSigma0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (bigSigma0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }

    hash[0] = (hash[0] + a) >>> 0;
    hash[1] = (hash[1] + b) >>> 0;
    hash[2] = (hash[2] + c) >>> 0;
    hash[3] = (hash[3] + d) >>> 0;
    hash[4] = (hash[4] + e) >>> 0;
    hash[5] = (hash[5] + f) >>> 0;
    hash[6] = (hash[6] + g) >>> 0;
    hash[7] = (hash[7] + h) >>> 0;

    const block = offset / 64;
    if ((block + 1) % FALLBACK_BLOCKS_PER_YIELD === 0 && offset + 64 < paddedLength) {
      await yieldToEventLoop();
      throwIfAborted(signal);
    }
  }

  throwIfAborted(signal);
  return hash.map((word) => word.toString(16).padStart(8, '0')).join('');
}

export async function sha256Hex(bytes: Uint8Array, signal?: AbortSignal): Promise<string> {
  throwIfAborted(signal);
  const subtle = globalThis.crypto?.subtle;
  if (subtle) {
    // WebCrypto's BufferSource excludes SharedArrayBuffer in the pinned DOM
    // types. Copy into an owned ArrayBuffer so both browser bytes and the
    // compile-time contract are unambiguous.
    const owned = new Uint8Array(bytes.byteLength);
    owned.set(bytes);
    const digest = await subtle.digest('SHA-256', owned.buffer);
    throwIfAborted(signal);
    return bytesToHex(new Uint8Array(digest));
  }
  return sha256Fallback(bytes, signal);
}
