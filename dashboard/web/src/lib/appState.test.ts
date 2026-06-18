import { describe, it, expect } from 'vitest';
import { deriveAppState } from './appState';
import type { Envelope } from '../types/envelope';

const FAKE_ENV = {} as Envelope;

describe('deriveAppState', () => {
  it('ready whenever a snapshot exists (even with a bootstrap error flag)', () => {
    expect(deriveAppState(FAKE_ENV, false)).toBe('ready');
    expect(deriveAppState(FAKE_ENV, true)).toBe('ready');
  });
  it('error when no snapshot and bootstrap failed', () => {
    expect(deriveAppState(null, true)).toBe('error');
  });
  it('loading when no snapshot and no error', () => {
    expect(deriveAppState(null, false)).toBe('loading');
  });
});
