import { describe, it, expect } from 'vitest';
import { railDateBucket, railSectionLabel } from './railDateBucket';
import type { ConversationSummary } from '../types/conversation';

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

function row(over: Partial<ConversationSummary>): ConversationSummary {
  return {
    session_id: 's1', title: 't', project_label: 'proj', git_branch: 'main',
    started_utc: '2026-06-09T01:00:00Z', last_activity_utc: '2026-06-09T01:00:00Z',
    msg_count: 1, cost_usd: 0, models: [], ...over,
  };
}

describe('railSectionLabel', () => {
  it('returns the date bucket for recent/oldest', () => {
    const r = row({ last_activity_utc: '2026-04-15T10:00:00Z' });
    expect(railSectionLabel('recent', r, TZ, NOW)).toBe('April 2026');
    expect(railSectionLabel('oldest', r, TZ, NOW)).toBe('April 2026');
  });
  it('returns null (flat list) for cost/messages', () => {
    const r = row({});
    expect(railSectionLabel('cost', r, TZ, NOW)).toBeNull();
    expect(railSectionLabel('messages', r, TZ, NOW)).toBeNull();
  });
  it('returns the project label for project sort', () => {
    expect(railSectionLabel('project', row({ project_label: 'cctally-dev' }), TZ, NOW)).toBe('cctally-dev');
  });
  it('falls back to em-dash for a blank project label under project sort', () => {
    expect(railSectionLabel('project', row({ project_label: '' }), TZ, NOW)).toBe('—');
  });
});
