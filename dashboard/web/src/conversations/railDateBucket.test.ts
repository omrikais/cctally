import { describe, it, expect } from 'vitest';
import { railDateBucket } from './railDateBucket';

const NOW = new Date('2026-06-09T12:00:00Z').getTime();
const TZ = 'UTC';

describe('railDateBucket', () => {
  it('buckets relative to now in the given tz', () => {
    expect(railDateBucket('2026-06-09T01:00:00Z', TZ, NOW)).toBe('Today');
    expect(railDateBucket('2026-06-08T23:00:00Z', TZ, NOW)).toBe('Yesterday');
    expect(railDateBucket('2026-06-05T10:00:00Z', TZ, NOW)).toBe('This Week');
    expect(railDateBucket('2026-06-01T10:00:00Z', TZ, NOW)).toBe('This Month');
    expect(railDateBucket('2026-04-15T10:00:00Z', TZ, NOW)).toBe('April 2026');
  });
});
