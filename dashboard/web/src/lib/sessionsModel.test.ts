import { describe, it, expect } from 'vitest';
import { singleModelLabel } from './sessionsModel';
import type { SessionRow } from '../types/envelope';

const row = (model: string): SessionRow => ({
  session_id: 's', started_utc: '2026-06-30T00:00:00Z', duration_min: 1,
  model, project: 'p', project_key: 'p', cost_usd: 1,
});

describe('singleModelLabel', () => {
  it('returns the abbreviated label when every row shares one real model', () => {
    expect(singleModelLabel([row('claude-opus-4-8'), row('claude-opus-4-8')])).toBe('opus-4-8');
  });
  it('returns null for a mixed set', () => {
    expect(singleModelLabel([row('claude-opus-4-8'), row('claude-sonnet-5')])).toBeNull();
  });
  it('returns null for an empty set', () => {
    expect(singleModelLabel([])).toBeNull();
  });
  it('returns null when any row model is blank / em-dash / (unknown)', () => {
    expect(singleModelLabel([row('claude-opus-4-8'), row('')])).toBeNull();
    expect(singleModelLabel([row('—')])).toBeNull();
    expect(singleModelLabel([row('(unknown)')])).toBeNull();
  });
  it('handles a single row', () => {
    expect(singleModelLabel([row('claude-sonnet-5')])).toBe('sonnet-5');
  });
});
