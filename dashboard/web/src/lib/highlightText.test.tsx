import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { HighlightText } from './highlightText';

function marks(el: HTMLElement): string[] {
  return Array.from(el.querySelectorAll('mark')).map((m) => m.textContent ?? '');
}

describe('HighlightText', () => {
  it('marks a single occurrence, preserving original casing', () => {
    const { container } = render(<HighlightText text="Scale-Employment" query="emp" />);
    expect(marks(container)).toEqual(['Emp']);
    expect(container.textContent).toBe('Scale-Employment');
  });
  it('marks multiple occurrences', () => {
    const { container } = render(<HighlightText text="aXaXa" query="a" />);
    expect(marks(container)).toEqual(['a', 'a', 'a']);
    expect(container.textContent).toBe('aXaXa');
  });
  it('is case-insensitive on the query', () => {
    const { container } = render(<HighlightText text="OPUS opus Opus" query="OpUs" />);
    expect(marks(container)).toEqual(['OPUS', 'opus', 'Opus']);
  });
  it('renders plain text when query is empty/whitespace', () => {
    const { container } = render(<HighlightText text="hello" query="   " />);
    expect(marks(container)).toEqual([]);
    expect(container.textContent).toBe('hello');
  });
  it('renders plain text when there is no match', () => {
    const { container } = render(<HighlightText text="hello" query="zz" />);
    expect(marks(container)).toEqual([]);
    expect(container.textContent).toBe('hello');
  });
  it('handles adjacent occurrences without dropping characters', () => {
    const { container } = render(<HighlightText text="abab" query="ab" />);
    expect(marks(container)).toEqual(['ab', 'ab']);
    expect(container.textContent).toBe('abab');
  });
});
