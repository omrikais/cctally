// CacheBreakdownCard regression tests for issue #77 P2-3.
//
// - Long project paths are middle-truncated to keep the leading
//   segment + tail visible; full path lives in title= for hover.
// - Short paths render unchanged with no title= attribute.
// - Model rows are never middle-truncated (kind='models' passes the
//   key through verbatim).
// - Key cells carry the .bd-key class so the CSS nowrap + ellipsis
//   fallback applies.
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { CacheBreakdownCard } from './CacheBreakdownCard';

describe('<CacheBreakdownCard /> path truncation (issue #77 P2-3)', () => {
  it('middle-truncates a moderately long path, keeping lead + tail', () => {
    // 46 chars; candidate1 (`/repos/…/feat/cache-report-panel`) = 32 chars,
    // still > the 28-char budget; candidate2 (`/repos/…/cache-report-panel`)
    // = 27 chars fits and preserves the lead segment.
    const path = '/repos/some/middle/segments/feat/cache-report-panel';
    const { container } = render(
      <CacheBreakdownCard
        kind="projects"
        rows={[{ key: path, cache_hit_percent: 60, net_usd: -0.5 }]}
      />,
    );
    const cell = container.querySelector('td.bd-key') as HTMLElement;
    expect(cell).toBeTruthy();
    // Display is shorter than the original.
    expect(cell.textContent!.length).toBeLessThan(path.length);
    // Middle-truncation marker present.
    expect(cell.textContent).toContain('…');
    // Both the leading segment AND the basename survive.
    expect(cell.textContent).toContain('/repos/');
    expect(cell.textContent).toContain('cache-report-panel');
    // Full path lives in title= for hover.
    expect(cell.getAttribute('title')).toBe(path);
  });

  it('drops the lead and keeps the basename when even lead+last-seg is too long', () => {
    // Length 78. Both candidate1 and candidate2 exceed 30 chars, so the
    // fallback path drops the lead and renders just …/<basename>. This
    // is the worst-case shape — we'd rather lose the volume than the
    // checkout name, which is what disambiguates worktrees.
    const path =
      '/Volumes/TRANSCEND/repos/cctally-dev/.worktrees/feature/view-model-unification';
    const { container } = render(
      <CacheBreakdownCard
        kind="projects"
        rows={[{ key: path, cache_hit_percent: 60, net_usd: -0.5 }]}
      />,
    );
    const cell = container.querySelector('td.bd-key') as HTMLElement;
    expect(cell.textContent).toContain('…');
    expect(cell.textContent).toContain('view-model-unification');
    expect(cell.textContent!.length).toBeLessThanOrEqual(28);
    expect(cell.getAttribute('title')).toBe(path);
  });

  it('short project paths render unchanged with no title attribute', () => {
    const shortPath = '/repos/cctally';
    const { container } = render(
      <CacheBreakdownCard
        kind="projects"
        rows={[{ key: shortPath, cache_hit_percent: 80, net_usd: 1.0 }]}
      />,
    );
    const cell = container.querySelector('td.bd-key') as HTMLElement;
    expect(cell.textContent).toBe(shortPath);
    expect(cell.getAttribute('title')).toBeNull();
  });

  it('model rows are never truncated (kind=models passes through verbatim)', () => {
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
