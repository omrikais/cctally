// PreviewPane — plan §M1.14 contract:
//   - 200ms debounce: a knob change does NOT fire renderShare
//     synchronously; advancing the fake timer past the debounce
//     resolves the fetch and renders the body.
//   - HTML / SVG bodies land in a sandboxed iframe with srcDoc set;
//     MD bodies render in a <pre> block.
//   - 400 errors surface as a red banner with the message + field.
//
// We control time via vi.useFakeTimers() so the 200ms debounce can be
// stepped over deterministically without sleeping the test runner.
import { render, screen, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PreviewPane } from './PreviewPane';
import type { ShareOptions } from './types';

// Mirrors production `defaultShareOptions()` in ShareModal.tsx — spec
// Q7 / §6.3: anon-by-default on export, so `reveal_projects: false`.
// (PreviewPane forces reveal_projects=true on its fetch regardless, so
// this is purely a setup default.)
function defaults(): ShareOptions {
  return {
    format: 'md',
    theme: 'light',
    reveal_projects: false,
    no_branding: false,
    top_n: 5,
    period: { kind: 'current' },
    project_allowlist: null,
    show_chart: true,
    show_table: true,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('<PreviewPane>', () => {
  it('renders MD bodies in a <pre> block after the debounce', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        body: '# weekly recap\n\n$12.34',
        content_type: 'text/markdown',
        snapshot: {},
      }),
    }));
    render(
      <PreviewPane
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
      />,
    );
    // Pre-debounce: in loading state, fetch not yet called.
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(0);

    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    // Now flush microtasks for the resolved promise to settle.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByLabelText(/markdown preview/i)).toHaveTextContent(
      'weekly recap',
    );
  });

  it('renders HTML in a sandboxed iframe with srcDoc', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        body: '<html><body>hello</body></html>',
        content_type: 'text/html',
        snapshot: {},
      }),
    }));
    const opts: ShareOptions = { ...defaults(), format: 'html' };
    const { container } = render(
      <PreviewPane panel="weekly" templateId="weekly-recap" options={opts} />,
    );
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const iframe = container.querySelector('iframe');
    expect(iframe).not.toBeNull();
    // Sandboxed, no scripting:
    expect(iframe!.getAttribute('sandbox')).toBe('allow-same-origin');
    expect(iframe!.getAttribute('srcdoc')).toContain('hello');
  });

  it('shows a red banner with field hint on 400 error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ error: 'unknown template_id', field: 'template_id' }),
    }));
    render(
      <PreviewPane
        panel="weekly"
        templateId="bogus"
        options={defaults()}
      />,
    );
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent(/unknown template_id/i);
    expect(alert).toHaveTextContent(/template_id/);
  });
});
