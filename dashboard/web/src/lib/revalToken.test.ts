import { describe, expect, it } from 'vitest';
import { revalToken } from './revalToken';
import type { Envelope } from '../types/envelope';

const env = (over: Partial<Envelope>) => over as Envelope;

describe('revalToken (#300)', () => {
  it('uses data_version when it is a non-empty string', () => {
    expect(revalToken(env({ data_version: '10.42.2.3.1.100.4.5', generated_at: 't1' })))
      .toBe('10.42.2.3.1.100.4.5');
  });

  it('falls back to generated_at when data_version is empty (the sentinel)', () => {
    expect(revalToken(env({ data_version: '', generated_at: 't1' }))).toBe('t1');
  });

  it('falls back to generated_at when data_version is absent', () => {
    expect(revalToken(env({ generated_at: 't2' }))).toBe('t2');
  });

  it('returns empty string when neither signal is present', () => {
    expect(revalToken(null)).toBe('');
    expect(revalToken(env({}))).toBe('');
  });

  it('is stable when only generated_at moves, and changes when data_version changes', () => {
    const a = revalToken(env({ data_version: 'v1', generated_at: 't1' }));
    const b = revalToken(env({ data_version: 'v1', generated_at: 't2' }));
    const c = revalToken(env({ data_version: 'v2', generated_at: 't2' }));
    expect(a).toBe(b);     // heartbeat advanced, change signal flat → same token
    expect(a).not.toBe(c); // change signal advanced → new token
  });
});
