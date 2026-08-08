// Task M3.6 — composer modal foundation (spec §8.2-§8.6).
//
// The modal subscribes to (composerModal slot, basket items) and posts
// to /api/share/compose with a 200ms debounce. Tests drive the reducer
// directly (BASKET_HYDRATE / openComposer) and fake the network with
// vi.spyOn(globalThis, 'fetch'). The recompose useEffect fires through
// setTimeout, so each "did we POST?" assertion uses waitFor to wait
// past the debounce.
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ComposerModal } from './ComposerModal';
import { _resetForTests, dispatch, getState } from '../store/store';
import { openComposer } from '../store/shareSlice';
import {
  installGlobalKeydown, _resetForTests as _resetKeymap,
} from '../store/keymap';
import type { BasketItem } from '../store/basketSlice';
import type { ShareOptions } from './types';

function defaultOpts(): ShareOptions {
  return {
    format: 'html', theme: 'light', reveal_projects: false,
    no_branding: false, top_n: 5, period: { kind: 'current' },
    project_allowlist: null, show_chart: true, show_table: true,
  };
}

function seedBasket(items: BasketItem[]) {
  dispatch({ type: 'BASKET_HYDRATE', items });
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  // Default: NOT mobile (the modal renders desktop unless overridden).
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {},
    dispatchEvent: () => false,
  }));
});

afterEach(() => {
  _resetKeymap();
  vi.restoreAllMocks();
});

describe('<ComposerModal>', () => {
  it('renders nothing when composer slot is empty', () => {
    const { container } = render(<ComposerModal />);
    expect(container.firstChild).toBeNull();
  });

  it('shows empty state when basket is empty', () => {
    dispatch(openComposer());
    render(<ComposerModal />);
    expect(screen.getByText(/basket is empty/i)).toBeInTheDocument();
  });

  it('fetches /api/share/compose on mount with non-empty basket', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        body: '<html><body><section>A</section></body></html>',
        content_type: 'text/html',
        snapshot: {
          kernel_version: 1,
          composed_at: '2026-05-11T09:00:00Z',
          section_results: [{
            snapshot_id: '00',
            drift_detected: false,
            data_digest_at_add: 'sha256:abc',
            data_digest_now: 'sha256:abc',
          }],
        },
      }),
    );
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: '2026-05-11T09:00:00Z',
      data_digest_at_add: 'sha256:abc', kernel_version: 1,
      label_hint: 'Weekly recap', source: 'claude' as const,
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledWith(
      '/api/share/compose',
      expect.objectContaining({ method: 'POST' }),
    ));
  });

  it('per-section Remove dispatches BASKET_REMOVE', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        body: '<html />', content_type: 'text/html',
        snapshot: {
          kernel_version: 1, composed_at: 't',
          section_results: [],
        },
      }),
    );
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: '2026-05-11T09:00:00Z',
      data_digest_at_add: 'sha256:abc', kernel_version: 1,
      label_hint: 'Weekly recap', source: 'claude' as const,
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    // Open the kebab first; the "Remove" entry lives inside the menu.
    fireEvent.click(screen.getByRole('button', { name: /actions for weekly recap/i }));
    fireEvent.click(screen.getByRole('button', { name: /remove weekly recap/i }));
    expect(getState().basket.items).toHaveLength(0);
  });

  it('Outdated badge shows when section drift_detected', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        body: '<html />', content_type: 'text/html',
        snapshot: {
          kernel_version: 1, composed_at: 't',
          section_results: [{
            snapshot_id: '00',
            drift_detected: true,
            data_digest_at_add: 'sha256:old',
            data_digest_now: 'sha256:new',
          }],
        },
      }),
    );
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't', data_digest_at_add: 'sha256:old',
      kernel_version: 1, label_hint: 'Weekly recap', source: 'claude' as const,
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    await waitFor(() => expect(screen.getByText(/outdated/i)).toBeInTheDocument());
  });

  it('real-name banner appears for anonymous-at-add sections under composite reveal', async () => {
    // Codex P1 regression on PR #35 — the prior AND-with-add-time
    // formula silently dropped this banner when every basket item
    // was added anonymously, even though the server forces composite
    // reveal across all sections at compose time. Now the banner
    // fires whenever composite reveal is enabled and any sections
    // are queued.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        body: '<html />', content_type: 'text/html',
        snapshot: { kernel_version: 1, composed_at: 't', section_results: [] },
      }),
    );
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      // Note: reveal_projects=false → "anonymous at add-time."
      options: { ...defaultOpts(), reveal_projects: false },
      added_at: 't', data_digest_at_add: 'sha256:abc',
      kernel_version: 1, label_hint: 'Weekly recap', source: 'claude' as const,
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    // Default "Anon on export" checked → composite reveal off → banner hidden.
    expect(screen.queryByText(/real project names/i)).toBeNull();
    // Uncheck "Anon on export" → composite reveal on → banner appears.
    const anonCheckbox = screen.getByLabelText(/anon on export/i) as HTMLInputElement;
    fireEvent.click(anonCheckbox);
    await waitFor(() => expect(screen.getByText(/real project names/i)).toBeInTheDocument());
  });

  it('real-name banner appears when a reveal-at-add section is present and anon-on-export unchecked', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        body: '<html />', content_type: 'text/html',
        snapshot: { kernel_version: 1, composed_at: 't', section_results: [] },
      }),
    );
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: { ...defaultOpts(), reveal_projects: true },
      added_at: 't', data_digest_at_add: 'sha256:abc',
      kernel_version: 1, label_hint: 'Weekly recap', source: 'claude' as const,
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    // Default anon-on-export is TRUE → composite reveal is FALSE → banner hidden.
    expect(screen.queryByText(/real project names/i)).toBeNull();
    // Uncheck "Anon on export" → composite reveal flips to TRUE → banner appears.
    const anonCheckbox = screen.getByLabelText(/anon on export/i) as HTMLInputElement;
    fireEvent.click(anonCheckbox);
    await waitFor(() => expect(screen.getByText(/real project names/i)).toBeInTheDocument());
    // Click "Anonymize all" → flips anon-on-export back ON → banner hides.
    fireEvent.click(screen.getByRole('button', { name: /anonymize all/i }));
    expect(screen.queryByText(/real project names/i)).toBeNull();
  });

  it('applies composer-modal-mobile class below 640px (spec §8.10)', () => {
    // Re-stub matchMedia to return true for the mobile breakpoint
    // query. useIsMobile reads the same query on first render via
    // useSyncExternalStore-style state init.
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q.includes('640'), media: q, onchange: null,
      addEventListener: () => {}, removeEventListener: () => {},
      addListener: () => {}, removeListener: () => {},
      dispatchEvent: () => false,
    }));
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't',
      data_digest_at_add: 'sha256:abc', kernel_version: 1,
      label_hint: 'W', source: 'claude' as const,
    }]);
    dispatch(openComposer());
    const { container } = render(<ComposerModal />);
    expect(container.querySelector('.composer-modal-mobile')).not.toBeNull();
  });

  it('omits composer-modal-mobile class on desktop', () => {
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't',
      data_digest_at_add: 'sha256:abc', kernel_version: 1,
      label_hint: 'W', source: 'claude' as const,
    }]);
    dispatch(openComposer());
    const { container } = render(<ComposerModal />);
    expect(container.querySelector('.composer-modal-mobile')).toBeNull();
    // But still has the base class.
    expect(container.querySelector('.composer-modal')).not.toBeNull();
  });

  it('Close button dispatches CLOSE_COMPOSER', () => {
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't', data_digest_at_add: 'sha256:abc',
      kernel_version: 1, label_hint: 'Weekly recap', source: 'claude' as const,
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    fireEvent.click(screen.getByRole('button', { name: /^close$/i }));
    expect(getState().composerModal).toBeNull();
  });

  it('Esc closes the composer (spec §12.1 MUST FIX regression)', () => {
    // Spec §12.1 mandates Esc closes any share/composer overlay. The
    // composer registers Esc at overlay scope with a `when:` gate
    // requiring composerModal !== null, so it fires only while the
    // composer is mounted-and-open.
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't', data_digest_at_add: 'sha256:abc',
      kernel_version: 1, label_hint: 'Weekly recap', source: 'claude' as const,
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    expect(getState().composerModal).not.toBeNull();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().composerModal).toBeNull();
  });

  it('Esc closes empty-state composer too', () => {
    // Empty basket renders the empty-state branch; the Esc binding is
    // registered before the early-return on closed, so it must still
    // fire when the user dismisses an empty composer. Explicitly seed
    // an empty basket to guard against localStorage carryover from
    // earlier tests in the file (BASKET_HYDRATE persists; the master
    // store re-reads on _resetForTests).
    seedBasket([]);
    dispatch(openComposer());
    render(<ComposerModal />);
    expect(screen.getByText(/basket is empty/i)).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().composerModal).toBeNull();
  });
});

// ---------------------------------------------------------------------
// M4 export actions (spec §8.8 / §11.5 row 511). The composer footer
// gets the same five buttons as ActionBar (Copy/Download/Open/PNG/
// Print), each doing a fresh POST to /api/share/compose with the right
// format override. Disable rules match ActionBar (Copy → md only;
// PNG → svg only; Print → html only; Open → html/svg only;
// Download → all). Each export sends the composite reveal_projects
// (inverse of the "Anon on export" checkbox).
// ---------------------------------------------------------------------

// jsdom doesn't implement URL.createObjectURL; tests that download / open
// a blob must stub it. We capture the prior descriptor so the stub
// doesn't leak between sibling test files in the same worker (the
// pattern ActionBar.test.tsx documents in detail).
const exportPropRefs: Array<[object, string, PropertyDescriptor | undefined]> = [];
function exportStubProperty<T extends object>(obj: T, key: string, value: unknown): void {
  exportPropRefs.push([obj, key, Object.getOwnPropertyDescriptor(obj, key)]);
  Object.defineProperty(obj, key, { value, configurable: true });
}

function exportRestoreProperties(): void {
  while (exportPropRefs.length > 0) {
    const [obj, key, desc] = exportPropRefs.pop()!;
    if (desc) Object.defineProperty(obj, key, desc);
    else delete (obj as Record<string, unknown>)[key];
  }
}

function exportComposeResp(body: string, contentType: string): Response {
  return jsonResponse({
    body,
    content_type: contentType,
    snapshot: {
      kernel_version: 1,
      composed_at: '2026-05-11T09:00:00Z',
      section_results: [{
        snapshot_id: '00',
        drift_detected: false,
        data_digest_at_add: 'sha256:abc',
        data_digest_now: 'sha256:abc',
      }],
    },
  });
}

function seedOneItemBasket(): void {
  seedBasket([{
    id: 'a', panel: 'weekly', template_id: 'weekly-recap',
    options: defaultOpts(), added_at: '2026-05-11T09:00:00Z',
    data_digest_at_add: 'sha256:abc', kernel_version: 1,
    label_hint: 'Weekly recap', source: 'claude' as const,
  }]);
}

describe('<ComposerModal> export actions (spec §8.8)', () => {
  afterEach(() => {
    exportRestoreProperties();
  });

  it('Copy is enabled only when format = md; click writes body to clipboard', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('# Composed report\n\nbody', 'text/markdown'),
    );
    const writeText = vi.fn().mockResolvedValue(undefined);
    exportStubProperty(navigator, 'clipboard', { writeText });
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);

    // Default format is html → Copy disabled with explanatory tooltip.
    const copy = screen.getByRole('button', { name: /^copy$/i });
    expect(copy).toBeDisabled();
    expect(copy.getAttribute('title')).toMatch(/markdown only/i);

    // Switch format to md → Copy enabled, click triggers a fresh
    // compose POST with format='md' and writes the body to clipboard.
    fireEvent.change(screen.getByLabelText(/^format$/i), { target: { value: 'md' } });
    await waitFor(() => expect(copy).not.toBeDisabled());

    await act(async () => {
      fireEvent.click(copy);
    });
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('# Composed report\n\nbody'));

    // The export call passed format='md' (independent of any preview format).
    const lastCall = fetchSpy.mock.calls[fetchSpy.mock.calls.length - 1];
    const bodyJson = JSON.parse((lastCall[1] as RequestInit).body as string);
    expect(bodyJson.format).toBe('md');
  });

  it('Download triggers a Blob + anchor click and respects composite reveal_projects', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('<html><body>report</body></html>', 'text/html'),
    );
    const createObjectURL = vi.fn().mockReturnValue('blob:fake-url');
    const revokeObjectURL = vi.fn();
    exportStubProperty(URL, 'createObjectURL', createObjectURL);
    exportStubProperty(URL, 'revokeObjectURL', revokeObjectURL);
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);

    // Default "Anon on export" is checked → composite reveal=false.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^download$/i }));
    });
    expect(createObjectURL).toHaveBeenCalled();
    expect(clickSpy).toHaveBeenCalled();

    // Composite reveal flows from !anonOnExport → false (checkbox on).
    // #503 S3 §3 — a composed export also POSTs one history row, so the
    // compose request is no longer simply the last call.
    const bodyJson = lastComposeBody(fetchSpy);
    expect(bodyJson.reveal_projects).toBe(false);
  });

  it('Open reserves a tab, navigates it, and records one composed row',
    async () => {
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
        exportComposeResp('<html>x</html>', 'text/html'),
      );
      const createObjectURL = vi.fn().mockReturnValue('blob:fake-url');
      exportStubProperty(URL, 'createObjectURL', createObjectURL);
      const tab = fakeExportTab();
      const openSpy = vi.spyOn(window, 'open').mockReturnValue(tab);
      seedOneItemBasket();
      dispatch(openComposer());
      render(<ComposerModal />);

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: /^open$/i }));
      });
      expect(openSpy).toHaveBeenCalledWith('', '_blank');
      expect(tab.location.href).toBe('blob:fake-url');
      const rows = composedHistoryPosts(fetchSpy);
      expect(rows).toHaveLength(1);
      expect(rows[0].kind).toBe('composed');
      expect(rows[0].destination).toBe('open');
      expect(rows[0].sections).toHaveLength(1);
    });

  it('a blocked Open reports failure and records no composed row', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('<html>x</html>', 'text/html'),
    );
    exportStubProperty(URL, 'createObjectURL', vi.fn().mockReturnValue('blob:x'));
    vi.spyOn(window, 'open').mockReturnValue(null);
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^open$/i }));
    });
    expect(screen.getByText(/blocked the new tab/i)).toBeInTheDocument();
    expect(composedHistoryPosts(fetchSpy)).toHaveLength(0);
  });

  it('Download records exactly one composed row', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('<html />', 'text/html'),
    );
    exportStubProperty(URL, 'createObjectURL', vi.fn().mockReturnValue('blob:x'));
    exportStubProperty(URL, 'revokeObjectURL', vi.fn());
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^download$/i }));
    });
    await waitFor(() =>
      expect(composedHistoryPosts(fetchSpy)).toHaveLength(1));
    const row = composedHistoryPosts(fetchSpy)[0];
    expect(row.kind).toBe('composed');
    expect(row.destination).toBe('download');
    expect(row.composite).toMatchObject({ reveal_projects: false });
  });

  it('Copy records exactly one composed row', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    exportStubProperty(navigator, 'clipboard', { writeText });
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('# Composed report', 'text/markdown'),
    );
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);

    fireEvent.change(screen.getByLabelText(/^format$/i), {
      target: { value: 'md' },
    });
    const copy = screen.getByRole('button', { name: /^copy$/i });
    await waitFor(() => expect(copy).not.toBeDisabled());
    await act(async () => { fireEvent.click(copy); });
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    await waitFor(() =>
      expect(composedHistoryPosts(fetchSpy)).toHaveLength(1));
    expect(composedHistoryPosts(fetchSpy)[0].destination).toBe('copy');
  });

  it('PNG records exactly one composed row', async () => {
    // The fifth handler §3 names. Copy, Download, Open and Print were
    // pinned; PNG was not, so `recordComposedHistory('png', 'svg')` was the
    // one append nothing executed.
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>',
        'image/svg+xml',
      ),
    );
    // The URL + Image + canvas surfaces svgToPng touches (ActionBar's PNG
    // test documents the same set).
    exportStubProperty(URL, 'createObjectURL', vi.fn().mockReturnValue('blob:x'));
    exportStubProperty(URL, 'revokeObjectURL', vi.fn());
    exportStubProperty(
      Image.prototype as object, 'decode', vi.fn().mockResolvedValue(undefined),
    );
    exportStubProperty(Image.prototype as object, 'naturalWidth', 50);
    exportStubProperty(Image.prototype as object, 'naturalHeight', 50);
    exportStubProperty(
      HTMLCanvasElement.prototype as object, 'toBlob',
      function (cb: BlobCallback) { cb(new Blob(['png'], { type: 'image/png' })); },
    );
    exportStubProperty(
      HTMLCanvasElement.prototype as object, 'getContext',
      () => ({
        fillRect: () => {}, scale: () => {}, drawImage: () => {}, fillStyle: '#fff',
      } as unknown as CanvasRenderingContext2D),
    );
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);

    fireEvent.change(screen.getByLabelText(/^format$/i), {
      target: { value: 'svg' },
    });
    const png = screen.getByRole('button', { name: /^png$/i });
    await waitFor(() => expect(png).not.toBeDisabled());
    await act(async () => { fireEvent.click(png); });

    expect(clickSpy).toHaveBeenCalled();
    await waitFor(() =>
      expect(composedHistoryPosts(fetchSpy)).toHaveLength(1));
    const row = composedHistoryPosts(fetchSpy)[0];
    expect(row.kind).toBe('composed');
    expect(row.destination).toBe('png');
    // The composed row records the format the SERVER produced, which for a
    // PNG is the SVG the rasterizer consumed.
    expect(row.format).toBe('svg');
  });

  it('a failed Print records no composed row', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('<html />', 'text/html'),
    );
    vi.spyOn(window, 'open').mockReturnValue(null);
    const origAppend = HTMLBodyElement.prototype.appendChild;
    vi.spyOn(HTMLBodyElement.prototype, 'appendChild').mockImplementation(
      function (this: HTMLBodyElement, node: Node) {
        const ret = origAppend.call(this, node) as Node;
        if (node instanceof HTMLIFrameElement && node.contentWindow) {
          Object.defineProperty(node.contentWindow, 'print', {
            value: () => { throw new Error('print blocked'); },
            configurable: true,
          });
        }
        return ret;
      });
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /print/i }));
    });
    expect(screen.getByText(/print failed/i)).toBeInTheDocument();
    expect(composedHistoryPosts(fetchSpy)).toHaveLength(0);
  });

  // #503 S3 §5 — the reserved-tab handle the composer navigates.
  function fakeExportTab() {
    return {
      location: { href: '' },
      opener: {} as unknown,
      close: vi.fn(),
    } as unknown as Window & { location: { href: string }; close: () => void };
  }

  function composeCalls(spy: { mock: { calls: unknown[][] } }) {
    return spy.mock.calls.filter(
      (c) => String(c[0]).startsWith('/api/share/compose'));
  }

  function lastComposeBody(spy: { mock: { calls: unknown[][] } }) {
    const calls = composeCalls(spy);
    return JSON.parse(
      (calls[calls.length - 1][1] as RequestInit).body as string,
    ) as Record<string, unknown>;
  }

  function composedHistoryPosts(spy: { mock: { calls: unknown[][] } }) {
    return spy.mock.calls
      .filter((c) => String(c[0]).startsWith('/api/share/history'))
      .map((c) => JSON.parse((c[1] as RequestInit).body as string));
  }

  it('PNG is format-gated to SVG; Print is format-gated to HTML (spec §8.8)', () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('<html />', 'text/html'),
    );
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);

    // Default format is html.
    const png = screen.getByRole('button', { name: /^png$/i });
    expect(png).toBeDisabled();
    expect(png.getAttribute('title')).toMatch(/svg format only/i);

    const print = screen.getByRole('button', { name: /print/i });
    // Print is enabled because default format is html.
    expect(print).not.toBeDisabled();

    // Switch to svg → PNG enabled, Print disabled with explanatory tooltip.
    fireEvent.change(screen.getByLabelText(/^format$/i), { target: { value: 'svg' } });
    expect(png).not.toBeDisabled();
    expect(print).toBeDisabled();
    expect(print.getAttribute('title')).toMatch(/html format only/i);
  });

  it('Export click forwards composite reveal_projects when anon-on-export is unchecked', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('<html />', 'text/html'),
    );
    const createObjectURL = vi.fn().mockReturnValue('blob:fake-url');
    exportStubProperty(URL, 'createObjectURL', createObjectURL);
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);

    // Uncheck "Anon on export" → composite reveal_projects flips to true.
    fireEvent.click(screen.getByLabelText(/anon on export/i));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^download$/i }));
    });
    expect(lastComposeBody(fetchSpy).reveal_projects).toBe(true);
  });

  it('Clear all asks first, then wipes the basket', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('<html />', 'text/html'),
    );
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);
    expect(getState().basket.items).toHaveLength(1);
    // #503 S3 §2 — the first click commits nothing. `Clear all` wrote
    // through to localStorage with no undo, so it names what it destroys.
    fireEvent.click(screen.getByRole('button', { name: /^clear all$/i }));
    expect(getState().basket.items).toHaveLength(1);
    expect(screen.getByText('Clear 1 section?')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Clear 1 section' }));
    expect(getState().basket.items).toHaveLength(0);
  });

  it('Clear all can be cancelled, and Escape cancels it too', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('<html />', 'text/html'),
    );
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);
    fireEvent.click(screen.getByRole('button', { name: /^clear all$/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    expect(getState().basket.items).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: /^clear all$/i }));
    await screen.findByText('Clear 1 section?');
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByText('Clear 1 section?')).not.toBeInTheDocument());
    expect(getState().basket.items).toHaveLength(1);
    // …and the composer itself is still open: the confirmation owned Escape,
    // it did not leak through to the modal's own binding.
    expect(getState().composerModal?.open).toBe(true);
  });
});


// #503 S3 §5 — the composer carries the same defect the share modal did:
// its export buttons never gated on, or mentioned, `composeErr`.
describe('#503 S3 §5 — the composer failed-preview note', () => {
  function note() {
    return document.querySelector('.share-preview-failed-note');
  }

  function exportButtons() {
    return Array.from(document.querySelectorAll<HTMLButtonElement>(
      '.composer-export-row .share-action'));
  }

  it('warns beside the actions without disabling them', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'compose exploded' }),
        { status: 500 }),
    );
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);

    await waitFor(() => expect(note()).not.toBeNull());
    expect(note()!.textContent).toBe('The preview failed — an export may fail too.');
    const id = note()!.getAttribute('id');
    for (const b of exportButtons()) {
      expect(b.getAttribute('aria-describedby')).toBe(id);
    }
    // Warn, do not disable — same rule as the share modal.
    expect(exportButtons().filter((b) => !b.disabled).length)
      .toBeGreaterThan(0);
    // Announced once: the composer keeps its own role="alert" banner.
    expect(note()!.getAttribute('aria-live')).toBeNull();
    expect(document.querySelector('.composer-error[role="alert"]'))
      .not.toBeNull();
  });

  it('shows no note while the compose preview is healthy', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      exportComposeResp('<html />', 'text/html'),
    );
    seedOneItemBasket();
    dispatch(openComposer());
    render(<ComposerModal />);
    await waitFor(() =>
      expect(document.querySelector('.composer-preview')).not.toBeNull());
    expect(note()).toBeNull();
  });
});
