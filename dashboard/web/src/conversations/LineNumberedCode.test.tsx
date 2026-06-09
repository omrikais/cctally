import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { LineNumberedCode, splitGutter } from './LineNumberedCode';

describe('splitGutter', () => {
  it('splits real cat -n lines into {num, content}', () => {
    expect(splitGutter('1\timport x\n2\tconst y = 1')).toEqual([
      { num: '1', content: 'import x' },
      { num: '2', content: 'const y = 1' },
    ]);
  });
  it('preserves a non-1-based (offset) start', () => {
    expect(splitGutter('7756\tdef f():').map((r) => r.num)).toEqual(['7756']);
  });
  it("keeps a content line's own leading tab", () => {
    expect(splitGutter('10\t\tindented')).toEqual([{ num: '10', content: '\tindented' }]);
  });
  it('a non-gutter line becomes a blank-gutter row (lossless)', () => {
    expect(splitGutter('<system-reminder>')).toEqual([{ num: '', content: '<system-reminder>' }]);
  });
});

describe('LineNumberedCode', () => {
  it('renders a gutter of numbers + token spans in the code column', () => {
    const { container } = render(
      <LineNumberedCode code={'1\tdef f():\n2\t    return 0'} lang="python" />,
    );
    expect(container.querySelector('.conv-code--numbered')).toBeInTheDocument();
    expect(container.querySelector('.cb-gutter')?.textContent).toBe('1\n2');
    expect(container.querySelector('.conv-code--numbered .token')).toBeInTheDocument();
  });
  it('falls back to the plain result <pre> when there is no gutter', () => {
    const { container } = render(<LineNumberedCode code={'File does not exist.'} lang="python" />);
    expect(container.querySelector('.conv-code--numbered')).toBeNull();
    const pre = container.querySelector('pre.conv-code--result');
    expect(pre?.textContent).toBe('File does not exist.');
    expect(container.querySelector('.token')).toBeNull();
  });
});
