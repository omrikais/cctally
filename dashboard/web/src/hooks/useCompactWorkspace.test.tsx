import { describe, it, expect, afterEach, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useCompactWorkspace } from './useCompactWorkspace';
import {
  COMPACT_WORKSPACE_MEDIA_QUERY,
  MOBILE_MEDIA_QUERY,
  WIDE_MEDIA_QUERY,
} from '../lib/breakpoints';
import { stubResponsiveMedia } from '../test-utils/mobileMedia';

afterEach(() => vi.unstubAllGlobals());

describe('useCompactWorkspace (#304 S1)', () => {
  it('is true at/below 880 (single-pane) and false above (two-pane)', () => {
    // Single-pane band 641–880: NOT mobile, IS compact-workspace, NOT wide.
    stubResponsiveMedia((q) => q === COMPACT_WORKSPACE_MEDIA_QUERY);
    const compact = renderHook(() => useCompactWorkspace());
    expect(compact.result.current).toBe(true);

    // Two-pane band 881+: compact-workspace query no longer matches.
    stubResponsiveMedia(() => false);
    const wide = renderHook(() => useCompactWorkspace());
    expect(wide.result.current).toBe(false);
  });

  it('keys on its OWN query, distinct from the 640 and 1100 queries', () => {
    // Mobile-only band (≤640): mobile true, but compact-workspace ALSO true
    // (880 ≥ 640). Prove the hook reads COMPACT_WORKSPACE_MEDIA_QUERY, not MOBILE.
    const seen: string[] = [];
    stubResponsiveMedia((q) => {
      seen.push(q);
      return q === COMPACT_WORKSPACE_MEDIA_QUERY;
    });
    const r = renderHook(() => useCompactWorkspace());
    expect(r.result.current).toBe(true);
    expect(seen).toContain(COMPACT_WORKSPACE_MEDIA_QUERY);
    expect(seen).not.toContain(MOBILE_MEDIA_QUERY);
    expect(seen).not.toContain(WIDE_MEDIA_QUERY);
  });
});
