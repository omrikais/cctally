import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { UnifiedDiffView } from './UnifiedDiffView';
import { parseUnifiedDiff } from './contextDiff';

// #217 S5 F6 — UnifiedDiffView renders a parsed git-context diff using the
// SAME row primitives as DiffCard (extracted into diffPrimitives.tsx), so an
// injected git diff looks byte-identical to an edit diff.
describe('UnifiedDiffView', () => {
  it('renders hunk rows for a parsed diff', () => {
    const files = parseUnifiedDiff('diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n');
    const { container } = render(<UnifiedDiffView files={files} />);
    // The shared row primitives emit the same add/del row classes as DiffCard.
    expect(container.querySelector('.conv-diff-row--add')).toBeTruthy();
    expect(container.querySelector('.conv-diff-row--del')).toBeTruthy();
    expect(container.querySelector('.conv-diff-hunk')).toBeTruthy();
  });

  it('shows a per-file path header', () => {
    const files = parseUnifiedDiff('diff --git a/dir/foo.py b/dir/foo.py\n@@ -1 +1 @@\n-a\n+b\n');
    const { getByText } = render(<UnifiedDiffView files={files} />);
    // basename rendered prominently.
    expect(getByText('foo.py')).toBeInTheDocument();
  });

  it('renders a +N −M stat per file', () => {
    const files = parseUnifiedDiff(
      'diff --git a/x b/x\n@@ -1,2 +1,3 @@\n ctx\n-old\n+a\n+b\n',
    );
    const { container } = render(<UnifiedDiffView files={files} />);
    const stat = container.querySelector('.conv-diff-stat');
    expect(stat?.textContent).toMatch(/\+2/);
    expect(stat?.textContent).toMatch(/−1|-1/);
  });

  it('renders one block per file for a multi-file diff', () => {
    const files = parseUnifiedDiff(
      'diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\ndiff --git a/y b/y\n@@ -1 +1 @@\n-c\n+d\n',
    );
    const { container } = render(<UnifiedDiffView files={files} />);
    expect(container.querySelectorAll('.conv-ctx-diff-file').length).toBe(2);
  });

  it('renders nothing for an empty file list', () => {
    const { container } = render(<UnifiedDiffView files={[]} />);
    expect(container.querySelector('.conv-diff-row--add')).toBeNull();
  });
});
