// Structural-accessibility regression for the dashboard shell (A2/A3/A7).
// Asserts the document landmarks (one <main>/<header>/<footer>), a single
// <h1> heading root, and the skip-link that targets the main region. Uses
// the real singleton store via _resetForTests (mirrors the other App-level
// integration tests — there is no renderWithStore shim in this codebase).
import { render } from '@testing-library/react';
import { describe, it, expect, beforeEach } from 'vitest';
import { App } from './App';
import { _resetForTests } from './store/store';

describe('App document structure', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('has one main landmark targeted by a skip-link, plus header/footer and a single h1', () => {
    const { container } = render(<App />);
    expect(container.querySelectorAll('main#main-content').length).toBe(1);
    expect(container.querySelector('main#main-content')?.getAttribute('tabindex')).toBe('-1');
    expect(container.querySelectorAll('header').length).toBe(1);
    expect(container.querySelectorAll('footer').length).toBe(1);
    expect(container.querySelectorAll('h1').length).toBe(1);
    const skip = container.querySelector('a.skip-link');
    expect(skip?.getAttribute('href')).toBe('#main-content');
  });
});
