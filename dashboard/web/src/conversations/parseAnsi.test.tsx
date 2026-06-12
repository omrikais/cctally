import { describe, it, expect } from 'vitest';
import { parseAnsi } from './parseAnsi';

describe('parseAnsi', () => {
  it('maps SGR fg colors and resets', () => {
    const spans = parseAnsi('plain \x1b[31mred\x1b[0m back');
    expect(spans).toEqual([
      { text: 'plain ', cls: null },
      { text: 'red', cls: 'ansi-red' },
      { text: ' back', cls: null },
    ]);
  });
  it('strips non-SGR escapes and passes plain text through', () => {
    expect(parseAnsi('a\x1b[2Kb')).toEqual([{ text: 'ab', cls: null }]);
    expect(parseAnsi('hello')).toEqual([{ text: 'hello', cls: null }]);
  });
  it('maps bright fg variants and a no-arg reset (\\x1b[m)', () => {
    expect(parseAnsi('\x1b[92mok\x1b[mtail')).toEqual([
      { text: 'ok', cls: 'ansi-grn' },
      { text: 'tail', cls: null },
    ]);
  });
  it('drops a span that becomes empty after stripping non-SGR escapes', () => {
    // The escape sits between two SGR markers, producing an empty middle span
    // that must be filtered out, not emitted as { text: '' }.
    expect(parseAnsi('\x1b[31m\x1b[2K\x1b[0mx')).toEqual([{ text: 'x', cls: null }]);
  });
  it('returns no spans for the empty string', () => {
    expect(parseAnsi('')).toEqual([]);
  });
});
