import { describe, expect, it } from 'vitest';
import { sidechainIndentClass } from './sidechainIndent';

describe('sidechainIndentClass', () => {
  it('returns no indent class at depth 0 (orphan AND main-spawned agents stay on the spine)', () => {
    expect(sidechainIndentClass(0)).toBe('');
  });

  it('returns the nested class at every depth >= 1 (true agent-in-agent nesting)', () => {
    expect(sidechainIndentClass(1)).toBe('conv-sidechain--nested');
    expect(sidechainIndentClass(2)).toBe('conv-sidechain--nested');
    expect(sidechainIndentClass(4)).toBe('conv-sidechain--nested');
    expect(sidechainIndentClass(9)).toBe('conv-sidechain--nested'); // arbitrary depth — magnitude is the --sc-depth calc, not a per-depth class
  });

  it('never emits --nested for depth 0 (the structural guarantee the A2 bug fix turns on)', () => {
    expect(sidechainIndentClass(0)).not.toContain('--nested');
  });
});
