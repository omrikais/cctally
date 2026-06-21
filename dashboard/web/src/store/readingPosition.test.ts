import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  READING_POS_CAP,
  READING_POS_KEY,
  clearReadingPositions,
  loadReadingPos,
  recordReadingPos,
} from './readingPosition';

// #217 S3 E1 — anchor-based reading-position memory: a bounded localStorage LRU
// map keyed by session_id, recording the current-turn uuid (NOT a pixel offset).

beforeEach(() => clearReadingPositions());
afterEach(() => clearReadingPositions());

describe('readingPosition persistence (#217 S3 E1)', () => {
  it('records and restores a position for a session', () => {
    expect(loadReadingPos('s1')).toBeNull();
    recordReadingPos('s1', 'u7', 1000);
    expect(loadReadingPos('s1')).toEqual({ uuid: 'u7', ts: 1000 });
  });

  it('a later record for the same session overwrites the prior uuid + ts', () => {
    recordReadingPos('s1', 'u1', 1000);
    recordReadingPos('s1', 'u2', 2000);
    expect(loadReadingPos('s1')).toEqual({ uuid: 'u2', ts: 2000 });
  });

  it('keeps positions for distinct sessions independent', () => {
    recordReadingPos('s1', 'u1', 1000);
    recordReadingPos('s2', 'u2', 2000);
    expect(loadReadingPos('s1')?.uuid).toBe('u1');
    expect(loadReadingPos('s2')?.uuid).toBe('u2');
  });

  it('caps the map at READING_POS_CAP, evicting the oldest by ts', () => {
    // Fill exactly the cap with ascending ts (s0 oldest … s49 newest).
    for (let i = 0; i < READING_POS_CAP; i++) recordReadingPos(`s${i}`, `u${i}`, 1000 + i);
    // All present.
    expect(loadReadingPos('s0')?.uuid).toBe('u0');
    expect(loadReadingPos(`s${READING_POS_CAP - 1}`)?.uuid).toBe(`u${READING_POS_CAP - 1}`);

    // One more (newer) session pushes past the cap → the OLDEST (s0) is evicted.
    recordReadingPos('sNew', 'uNew', 9_999_999);
    expect(loadReadingPos('s0')).toBeNull();           // evicted (smallest ts)
    expect(loadReadingPos('sNew')?.uuid).toBe('uNew');  // the new one survives
    expect(loadReadingPos('s1')?.uuid).toBe('u1');      // a still-recent one survives

    // Exactly the cap remains.
    const raw = JSON.parse(localStorage.getItem(READING_POS_KEY)!);
    expect(Object.keys(raw)).toHaveLength(READING_POS_CAP);
  });

  it('re-recording an old session refreshes its recency so it is not evicted', () => {
    for (let i = 0; i < READING_POS_CAP; i++) recordReadingPos(`s${i}`, `u${i}`, 1000 + i);
    // Touch the oldest (s0) with a fresh ts so it becomes the NEWEST.
    recordReadingPos('s0', 'u0b', 9_000_000);
    // Now add a new session — the evicted one should be s1 (now the oldest), not s0.
    recordReadingPos('sNew', 'uNew', 9_999_999);
    expect(loadReadingPos('s0')?.uuid).toBe('u0b'); // survived (refreshed)
    expect(loadReadingPos('s1')).toBeNull();        // evicted (now-oldest)
  });

  it('returns null for an unknown session and ignores empty inputs', () => {
    expect(loadReadingPos('nope')).toBeNull();
    recordReadingPos('', 'u1', 1);
    recordReadingPos('s1', '', 1);
    expect(localStorage.getItem(READING_POS_KEY)).toBeNull();
  });

  it('tolerates corrupt localStorage (bad JSON) by reading as empty', () => {
    localStorage.setItem(READING_POS_KEY, '{not json');
    expect(loadReadingPos('s1')).toBeNull();
    // A subsequent record overwrites the corrupt blob with a valid map.
    recordReadingPos('s1', 'u1', 1000);
    expect(loadReadingPos('s1')?.uuid).toBe('u1');
  });
});
