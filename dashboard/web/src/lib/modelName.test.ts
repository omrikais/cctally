import { describe, it, expect } from 'vitest';
import { abbreviateModel } from './modelName';

describe('abbreviateModel', () => {
  it('strips the claude- prefix and a trailing -YYYYMMDD date stamp', () => {
    expect(abbreviateModel('claude-haiku-4-5-20251001')).toBe('haiku-4-5');
    expect(abbreviateModel('claude-sonnet-4-5-20250929')).toBe('sonnet-4-5');
  });

  it('strips the prefix when there is no date stamp', () => {
    expect(abbreviateModel('claude-opus-4-8')).toBe('opus-4-8');
  });

  it('passes an unrecognized shape through unchanged', () => {
    expect(abbreviateModel('gpt-5')).toBe('gpt-5');
  });

  it('never returns an empty string (fail-safe on a degenerate input)', () => {
    expect(abbreviateModel('claude-')).toBe('claude-');
    expect(abbreviateModel('-20251001')).toBe('-20251001');
  });
});
