// Automated share-v2 manual-smoke coverage.
//
// This file turns the highest-value parts of docs/commands/share-v2.md's
// manual checklist into fast jsdom flows:
//   - per-share export actions record non-empty exports + recent history;
//   - basket/composer sections persist, reorder, refresh, export, and clear;
//   - presets and recent-history recipes round-trip across a "page reload".
//
// Lower-level tests still own fine-grained edge cases. These are deliberately
// broad enough to catch wiring regressions that otherwise require a real
// browser smoke pass.
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ActionBar } from './ActionBar';
import { ComposerModal } from './ComposerModal';
import { ShareModalRoot } from './ShareModalRoot';
import { closeShareModal, openComposer, openShareModal } from '../store/shareSlice';
import { _resetForTests, dispatch, getState } from '../store/store';
import { BASKET_STORAGE_KEY } from '../store/basketSlice';
import type { BasketItem } from '../store/basketSlice';
import type {
  ShareFormat,
  ShareOptions,
  SharePanelId,
  ShareTemplate,
} from './types';

const pngMocks = vi.hoisted(() => ({
  svgToPng: vi.fn(),
}));
const printMocks = vi.hoisted(() => ({
  printPdf: vi.fn(),
}));

vi.mock('./exporters/png', () => ({
  svgToPng: pngMocks.svgToPng,
}));
vi.mock('./exporters/printPdf', () => ({
  printPdf: printMocks.printPdf,
}));

type RenderRequest = {
  panel: SharePanelId;
  template_id: string;
  options: ShareOptions;
};

type ComposeRequest = {
  title: string;
  theme: 'light' | 'dark';
  format: ShareFormat;
  no_branding: boolean;
  reveal_projects: boolean;
  sections: Array<{
    snapshot: {
      panel: SharePanelId;
      template_id: string;
      options: ShareOptions;
      data_digest_at_add: string;
      kernel_version: number;
    };
  }>;
};

type PresetRecord = {
  template_id: string;
  options: ShareOptions;
  saved_at: string;
};

type HistoryRecord = {
  recipe_id: string;
  panel: SharePanelId;
  template_id: string;
  options: ShareOptions;
  format: string | null;
  destination: string | null;
  exported_at: string;
};

function defaults(overrides: Partial<ShareOptions> = {}): ShareOptions {
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
    ...overrides,
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input;
  if (input instanceof URL) return `${input.pathname}${input.search}`;
  return input.url;
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method) return init.method.toUpperCase();
  if (typeof input !== 'string' && !(input instanceof URL)) return input.method.toUpperCase();
  return 'GET';
}

function contentTypeFor(format: ShareFormat): string {
  switch (format) {
    case 'md':
      return 'text/markdown';
    case 'html':
      return 'text/html';
    case 'svg':
      return 'image/svg+xml';
  }
}

function bodyFor(format: ShareFormat, label: string): string {
  switch (format) {
    case 'md':
      return `# ${label}\n\nnon-empty markdown body`;
    case 'html':
      return `<!DOCTYPE html><html><body><h1>${label}</h1></body></html>`;
    case 'svg':
      return `<svg width="640" height="360" viewBox="0 0 640 360"><text>${label}</text></svg>`;
  }
}

class ShareSmokeServer {
  presets: Record<string, Record<string, PresetRecord>> = {};
  history: HistoryRecord[] = [];
  renderRequests: RenderRequest[] = [];
  composeRequests: ComposeRequest[] = [];
  historyPosts: Array<Omit<HistoryRecord, 'recipe_id' | 'exported_at'>> = [];

  fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = requestUrl(input);
    const method = requestMethod(input, init);

    if (method === 'GET' && url.startsWith('/api/share/templates')) {
      const panel = new URL(url, 'http://local.test').searchParams.get('panel') as SharePanelId;
      return jsonResponse({ panel, templates: this.templatesFor(panel) });
    }

    if (method === 'POST' && url === '/api/share/render') {
      const req = JSON.parse(String(init?.body ?? '{}')) as RenderRequest;
      this.renderRequests.push(req);
      const digest = `sha256:${req.panel}-${this.renderRequests.length}`;
      return jsonResponse({
        body: bodyFor(req.options.format, req.template_id),
        content_type: contentTypeFor(req.options.format),
        snapshot: {
          kernel_version: 1,
          panel: req.panel,
          template_id: req.template_id,
          options: req.options,
          generated_at: '2026-05-12T09:00:00Z',
          data_digest: digest,
        },
      });
    }

    if (method === 'GET' && url === '/api/share/presets') {
      return jsonResponse({ presets: this.presets });
    }

    if (method === 'POST' && url === '/api/share/presets') {
      const req = JSON.parse(String(init?.body ?? '{}')) as {
        panel: SharePanelId;
        name: string;
        template_id: string;
        options: ShareOptions;
      };
      this.presets[req.panel] ??= {};
      this.presets[req.panel][req.name] = {
        template_id: req.template_id,
        options: req.options,
        saved_at: '2026-05-12T09:05:00Z',
      };
      return jsonResponse({
        panel: req.panel,
        name: req.name,
        ...this.presets[req.panel][req.name],
      });
    }

    if (method === 'GET' && url === '/api/share/history') {
      return jsonResponse({ history: this.history });
    }

    if (method === 'POST' && url === '/api/share/history') {
      const req = JSON.parse(String(init?.body ?? '{}')) as Omit<HistoryRecord, 'recipe_id' | 'exported_at'>;
      this.historyPosts.push(req);
      const record: HistoryRecord = {
        ...req,
        recipe_id: `history-${this.history.length + 1}`,
        exported_at: `2026-05-12T09:${String(this.history.length).padStart(2, '0')}:00Z`,
      };
      this.history.push(record);
      this.history = this.history.slice(-20);
      return jsonResponse(record);
    }

    if (method === 'POST' && url === '/api/share/compose') {
      const req = JSON.parse(String(init?.body ?? '{}')) as ComposeRequest;
      this.composeRequests.push(req);
      return jsonResponse({
        body: bodyFor(req.format, `composed-${req.sections.length}`),
        content_type: contentTypeFor(req.format),
        snapshot: {
          kernel_version: 1,
          composed_at: '2026-05-12T09:10:00Z',
          section_results: req.sections.map((section, idx) => ({
            snapshot_id: String(idx).padStart(2, '0'),
            drift_detected: false,
            data_digest_at_add: section.snapshot.data_digest_at_add,
            data_digest_now: section.snapshot.data_digest_at_add,
          })),
        },
      });
    }

    return jsonResponse({ error: `unhandled ${method} ${url}` }, 500);
  });

  templatesFor(panel: SharePanelId): ShareTemplate[] {
    return (['recap', 'visual', 'detail'] as const).map((archetype) => ({
      id: `${panel}-${archetype}`,
      label: archetype[0].toUpperCase() + archetype.slice(1),
      description: `${panel} ${archetype} template`,
      default_options: {
        format: 'md',
        theme: 'light',
        top_n: panel === 'sessions' ? 15 : 5,
        period: { kind: 'current' },
      },
    }));
  }
}

const propRefs: Array<[object, string, PropertyDescriptor | undefined]> = [];

function stubProperty<T extends object>(obj: T, key: string, value: unknown): void {
  propRefs.push([obj, key, Object.getOwnPropertyDescriptor(obj, key)]);
  Object.defineProperty(obj, key, { value, configurable: true });
}

function restoreProperties(): void {
  while (propRefs.length > 0) {
    const [obj, key, desc] = propRefs.pop()!;
    if (desc) Object.defineProperty(obj, key, desc);
    else delete (obj as Record<string, unknown>)[key];
  }
}

let server: ShareSmokeServer;
let anchorClickSpy: ReturnType<typeof vi.spyOn>;
let windowOpenSpy: ReturnType<typeof vi.spyOn>;
let clipboardWrite: ReturnType<typeof vi.fn>;

beforeEach(() => {
  cleanup();
  localStorage.clear();
  _resetForTests();
  server = new ShareSmokeServer();
  vi.stubGlobal('fetch', server.fetch);
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));

  clipboardWrite = vi.fn().mockResolvedValue(undefined);
  stubProperty(navigator, 'clipboard', { writeText: clipboardWrite });
  stubProperty(URL, 'createObjectURL', vi.fn().mockReturnValue('blob:share-smoke'));
  stubProperty(URL, 'revokeObjectURL', vi.fn());
  anchorClickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  windowOpenSpy = vi.spyOn(window, 'open').mockReturnValue(null);
  pngMocks.svgToPng.mockReset();
  pngMocks.svgToPng.mockResolvedValue(new Blob(['png-bytes'], { type: 'image/png' }));
  printMocks.printPdf.mockReset();
});

afterEach(() => {
  cleanup();
  restoreProperties();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

async function openWeeklyShareModal() {
  render(<ShareModalRoot />);
  await act(async () => {
    dispatch(openShareModal('weekly', null));
  });
  await screen.findByRole('heading', { name: /share weekly report/i });
  await screen.findByRole('radio', { name: /recap/i });
}

async function waitForPreviewRender(): Promise<RenderRequest> {
  await waitFor(() => {
    expect(server.renderRequests.length).toBeGreaterThan(0);
  });
  return server.renderRequests[server.renderRequests.length - 1];
}

async function clickExport(name: RegExp | string): Promise<void> {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name }));
  });
}

describe('share-v2 manual smoke harness', () => {
  it('covers template switching plus MD/HTML/SVG export actions and recent-history writes', async () => {
    await openWeeklyShareModal();

    // Preview renders even while Anon on export is checked, and it always
    // reveals projects independently of the export checkbox.
    const initialPreview = await waitForPreviewRender();
    expect(initialPreview.options.reveal_projects).toBe(true);

    // Cycle Recap -> Visual -> Detail like the manual smoke asks.
    fireEvent.click(screen.getByRole('radio', { name: /visual/i }));
    fireEvent.click(screen.getByRole('radio', { name: /detail/i }));
    expect(screen.getByRole('radio', { name: /detail/i })).toHaveAttribute('aria-checked', 'true');

    // MD -> Copy: non-empty clipboard body, anonymized export recipe,
    // and a recent-history POST.
    await clickExport(/^copy$/i);
    await waitFor(() => expect(clipboardWrite).toHaveBeenCalledWith(expect.stringContaining('non-empty')));
    await waitFor(() => expect(server.historyPosts).toHaveLength(1));
    expect(server.historyPosts[0]).toMatchObject({
      panel: 'weekly',
      template_id: 'weekly-detail',
      destination: 'copy',
      format: 'md',
    });
    expect(server.historyPosts[0].options.reveal_projects).toBe(false);

    // Toggle Anon off so the next export proves the checkbox is honored.
    fireEvent.click(screen.getByLabelText(/anonymize project names on export/i));

    // HTML -> Open + Print.
    fireEvent.click(screen.getByLabelText('html'));
    await clickExport(/^open$/i);
    await waitFor(() => expect(windowOpenSpy).toHaveBeenCalledWith(
      'blob:share-smoke',
      '_blank',
      'noopener,noreferrer',
    ));
    await waitFor(() => expect(server.historyPosts).toHaveLength(2));
    expect(server.historyPosts[1].options.reveal_projects).toBe(true);

    await clickExport(/print/i);
    await waitFor(() => expect(printMocks.printPdf).toHaveBeenCalledWith(expect.stringContaining('<!DOCTYPE html>')));
    await waitFor(() => expect(server.historyPosts).toHaveLength(3));

    // SVG -> Download + PNG.
    fireEvent.click(screen.getByLabelText('svg'));
    await clickExport(/^download$/i);
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalled());
    await waitFor(() => expect(server.historyPosts).toHaveLength(4));
    expect(server.historyPosts[3].destination).toBe('download');
    expect(server.historyPosts[3].format).toBe('svg');

    await clickExport(/^png$/i);
    await waitFor(() => expect(pngMocks.svgToPng).toHaveBeenCalledWith(
      expect.stringContaining('<svg'),
      2,
      '#ffffff',
    ));
    await waitFor(() => expect(server.historyPosts).toHaveLength(5));
    expect(server.historyPosts.map((h) => h.destination)).toEqual([
      'copy',
      'open',
      'print',
      'download',
      'png',
    ]);
  });

  it('covers basket persistence, composer reorder/remove/refresh, export, and clear-all', async () => {
    render(
      <>
        <section aria-label="weekly action bar">
          <ActionBar
            panel="weekly"
            templateId="weekly-recap"
            options={defaults({ reveal_projects: true })}
            onOptionsChange={() => {}}
          />
        </section>
        <section aria-label="daily action bar">
          <ActionBar
            panel="daily"
            templateId="daily-visual"
            options={defaults({ format: 'html' })}
            onOptionsChange={() => {}}
          />
        </section>
        <section aria-label="forecast action bar">
          <ActionBar
            panel="forecast"
            templateId="forecast-detail"
            options={defaults({ format: 'svg' })}
            onOptionsChange={() => {}}
          />
        </section>
      </>,
    );

    for (const label of ['weekly action bar', 'daily action bar', 'forecast action bar']) {
      const root = screen.getByRole('region', { name: label });
      await act(async () => {
        fireEvent.click(within(root).getByRole('button', { name: /\+ basket/i }));
      });
    }
    await waitFor(() => expect(getState().basket.items).toHaveLength(3));
    expect(JSON.parse(localStorage.getItem(BASKET_STORAGE_KEY) ?? '[]')).toHaveLength(3);

    dispatch({ type: 'BASKET_REORDER', fromIdx: 0, toIdx: 2 });
    const persistedOrder = (JSON.parse(localStorage.getItem(BASKET_STORAGE_KEY) ?? '[]') as BasketItem[])
      .map((it) => it.panel);
    expect(persistedOrder).toEqual(['daily', 'forecast', 'weekly']);

    cleanup();
    render(<ComposerModal />);
    await act(async () => {
      dispatch(openComposer());
    });
    await screen.findByRole('dialog', { name: /compose report/i });
    await waitFor(() => expect(server.composeRequests.length).toBeGreaterThan(0));
    expect(screen.queryByText(/outdated/i)).not.toBeInTheDocument();

    // Toggle composite anon off and back on. The server reports no drift,
    // so rows must not sprout Outdated badges merely because privacy mode
    // changed.
    const anon = screen.getByLabelText(/anon on export/i);
    fireEvent.click(anon);
    await waitFor(() => expect(server.composeRequests.at(-1)?.reveal_projects).toBe(true));
    fireEvent.click(anon);
    await waitFor(() => expect(server.composeRequests.at(-1)?.reveal_projects).toBe(false));
    expect(screen.queryByText(/outdated/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /actions for daily/i }));
    fireEvent.click(screen.getByRole('button', { name: /remove daily/i }));
    expect(getState().basket.items.map((it) => it.panel)).toEqual(['forecast', 'weekly']);

    fireEvent.click(screen.getByRole('button', { name: /actions for forecast/i }));
    fireEvent.click(screen.getByRole('button', { name: /refresh from current data/i }));
    await waitFor(() => {
      const refreshed = getState().basket.items.find((it) => it.panel === 'forecast');
      expect(refreshed?.data_digest_at_add).toMatch(/^sha256:forecast-/);
    });

    await clickExport(/^download$/i);
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalled());
    expect(server.composeRequests.at(-1)).toMatchObject({
      format: 'html',
      reveal_projects: false,
    });

    await clickExport(/print/i);
    await waitFor(() => expect(printMocks.printPdf).toHaveBeenCalledWith(expect.stringContaining('<!DOCTYPE html>')));

    fireEvent.click(screen.getByRole('button', { name: /clear all/i }));
    expect(getState().basket.items).toHaveLength(0);
    expect(JSON.parse(localStorage.getItem(BASKET_STORAGE_KEY) ?? '[]')).toEqual([]);
  });

  it('round-trips a saved preset and recent share across a reload', async () => {
    await openWeeklyShareModal();

    fireEvent.click(screen.getByLabelText('Dark'));
    fireEvent.change(screen.getByLabelText(/^top-n$/i), { target: { value: '10' } });
    await clickExport(/^copy$/i);
    await waitFor(() => expect(server.history).toHaveLength(1));

    fireEvent.click(screen.getByRole('button', { name: /save preset/i }));
    fireEvent.change(screen.getByLabelText(/preset name/i), {
      target: { value: 'team-monday' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(server.presets.weekly?.['team-monday']).toBeTruthy());
    expect(server.presets.weekly['team-monday'].options).toMatchObject({
      theme: 'dark',
      top_n: 10,
      reveal_projects: false,
    });

    // Emulate a page refresh: React tree and UI store reset, server-side
    // presets/history remain available through the fake API.
    cleanup();
    _resetForTests();
    render(<ShareModalRoot />);
    await act(async () => {
      dispatch(openShareModal('weekly', null));
    });
    await screen.findByRole('heading', { name: /share weekly report/i });

    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    const preset = await screen.findByRole('menuitem', { name: 'team-monday' });
    expect(screen.getByText(/recent shares/i)).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /weekly-recap/i })).toBeInTheDocument();

    fireEvent.click(preset);
    expect(screen.getByLabelText('Dark')).toBeChecked();
    expect(screen.getByLabelText(/^top-n$/i)).toHaveValue(10);

    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    const recent = await screen.findByRole('menuitem', { name: /weekly-recap/i });
    fireEvent.click(recent);
    expect(screen.getByLabelText('Dark')).toBeChecked();
    expect(screen.getByLabelText(/^top-n$/i)).toHaveValue(10);

    await act(async () => {
      dispatch(closeShareModal());
    });
  });
});
