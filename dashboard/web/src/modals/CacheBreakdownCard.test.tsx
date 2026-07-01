// CacheBreakdownCard CR-4 (#251): the By-project card shows basenames
// ("cctally-dev") with the full path preserved in title= for hover-to-reveal,
// replacing the earlier middle-truncation (#77). Model rows pass through
// verbatim (kind='models' is never basenamed). Key cells keep the .bd-key
// class so the CSS nowrap + ellipsis fallback still applies.
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { CacheBreakdownCard } from './CacheBreakdownCard';

describe('<CacheBreakdownCard /> project basenames (#251 CR-4)', () => {
  it('shows the basename of a full project path with the full path in title', () => {
    const path = '/Volumes/TRANSCEND/repos/cctally-dev';
    const { container } = render(
      <CacheBreakdownCard
        kind="projects"
        rows={[{ key: path, cache_hit_percent: 96, net_usd: 10 }]}
      />,
    );
    const cell = container.querySelector('td.bd-key') as HTMLElement;
    expect(cell.textContent).toBe('cctally-dev');
    expect(cell.getAttribute('title')).toBe(path);
  });

  it('shows the basename of a deep worktree path (keeps the checkout name)', () => {
    const path =
      '/Volumes/TRANSCEND/repos/cctally-dev/.worktrees/feature/view-model-unification';
    const { container } = render(
      <CacheBreakdownCard
        kind="projects"
        rows={[{ key: path, cache_hit_percent: 60, net_usd: -0.5 }]}
      />,
    );
    const cell = container.querySelector('td.bd-key') as HTMLElement;
    expect(cell.textContent).toBe('view-model-unification');
    expect(cell.getAttribute('title')).toBe(path);
  });

  it('single-segment project keys carry no redundant title', () => {
    const { container } = render(
      <CacheBreakdownCard
        kind="projects"
        rows={[{ key: 'cctally-dev', cache_hit_percent: 80, net_usd: 1.0 }]}
      />,
    );
    const cell = container.querySelector('td.bd-key') as HTMLElement;
    expect(cell.textContent).toBe('cctally-dev');
    expect(cell.getAttribute('title')).toBeNull();
  });

  it('model rows pass through verbatim (kind=models never basenamed)', () => {
    const longModel = 'claude-haiku-4-5-20251001';
    const { container } = render(
      <CacheBreakdownCard
        kind="models"
        rows={[{ key: longModel, cache_hit_percent: 75, net_usd: 0.3 }]}
      />,
    );
    const cell = container.querySelector('td.bd-key') as HTMLElement;
    expect(cell.textContent).toBe(longModel);
    expect(cell.getAttribute('title')).toBeNull();
  });

  it('every key cell carries the .bd-key class so nowrap CSS applies', () => {
    const { container } = render(
      <CacheBreakdownCard
        kind="projects"
        rows={[
          { key: 'a', cache_hit_percent: 60, net_usd: 0.1 },
          { key: 'b', cache_hit_percent: 70, net_usd: 0.2 },
        ]}
      />,
    );
    const cells = container.querySelectorAll('td.bd-key');
    expect(cells.length).toBe(2);
  });
});
