import { describe, it, expect } from 'vitest';
import { allOneProject, visibleBadges } from './railDiscovery';
import type { ConversationSummary } from '../types/conversation';

const row = (project_label: string): ConversationSummary => ({
  session_id: project_label, title: 't', project_label, git_branch: null,
  started_utc: '', last_activity_utc: '', msg_count: 0, cost_usd: 0, models: [],
});

describe('allOneProject', () => {
  it('empty list → true (nothing to disambiguate)', () => {
    expect(allOneProject([])).toBe(true);
  });
  it('all rows share one project → true', () => {
    expect(allOneProject([row('A'), row('A'), row('A')])).toBe(true);
  });
  it('two distinct projects → false', () => {
    expect(allOneProject([row('A'), row('B')])).toBe(false);
  });
});

describe('visibleBadges', () => {
  it('drops the badge that echoes a single-kind facet', () => {
    expect(visibleBadges(['tool'], 'tools')).toEqual([]);
    expect(visibleBadges(['file'], 'files')).toEqual([]);
    expect(visibleBadges(['title'], 'title')).toEqual([]);
    expect(visibleBadges(['thinking'], 'thinking')).toEqual([]);
  });
  it('keeps badges in the multi-kind All view', () => {
    expect(visibleBadges(['tool', 'thinking'], 'all')).toEqual(['tool', 'thinking']);
  });
});
