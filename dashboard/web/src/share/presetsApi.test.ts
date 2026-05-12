// Plan §M2.4 step 1 — typed wrappers around /api/share/presets.
// Plan §M4.3 step 5 — typed wrappers around /api/share/history.
import { describe, expect, it, vi, afterEach } from 'vitest';
import {
  listPresets,
  savePreset,
  deletePreset,
  listHistory,
  appendHistory,
  clearHistory,
  ShareApiError,
} from './presetsApi';
import type { ShareOptions } from './types';

afterEach(() => vi.restoreAllMocks());

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

describe('presetsApi', () => {
  it('listPresets GETs /api/share/presets', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        presets: {
          weekly: {
            x: {
              template_id: 'weekly-recap',
              options: defaults(),
              saved_at: '2026-05-11T09:00:00Z',
            },
          },
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    const out = await listPresets();
    expect(fetchSpy).toHaveBeenCalledWith('/api/share/presets', expect.any(Object));
    expect(out.presets.weekly.x.template_id).toBe('weekly-recap');
  });

  it('savePreset POSTs body as JSON', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        panel: 'weekly',
        name: 'm',
        template_id: 'weekly-recap',
        options: defaults(),
        saved_at: '2026-05-11T09:00:00Z',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    await savePreset({
      panel: 'weekly',
      name: 'm',
      template_id: 'weekly-recap',
      options: defaults(),
    });
    expect(fetchSpy).toHaveBeenCalledWith('/api/share/presets', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
    }));
    const call = fetchSpy.mock.calls[0];
    const opts = call[1] as RequestInit;
    expect(JSON.parse(opts.body as string).name).toBe('m');
  });

  it('deletePreset DELETEs to /api/share/presets/<panel>/<name>', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    await deletePreset('weekly', 'm');
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/share/presets/weekly/m',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('deletePreset URL-encodes path segments', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    // Names with spaces / special chars must survive the round-trip.
    await deletePreset('weekly', 'team monday');
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/share/presets/weekly/team%20monday',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('listPresets surfaces server errors as ShareApiError', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'boom' }), { status: 500 }),
    );
    await expect(listPresets()).rejects.toBeInstanceOf(ShareApiError);
  });

  it('deletePreset surfaces server errors as ShareApiError', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'no such preset' }), { status: 404 }),
    );
    await expect(deletePreset('weekly', 'nope')).rejects.toBeInstanceOf(ShareApiError);
  });

  // ---- M4.3 — /api/share/history wrappers ----

  it('listHistory GETs /api/share/history', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        history: [
          {
            recipe_id: 'abc123',
            panel: 'weekly',
            template_id: 'weekly-recap',
            options: defaults(),
            format: 'md',
            destination: 'download',
            exported_at: '2026-05-11T10:00:00Z',
          },
        ],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    const out = await listHistory();
    expect(fetchSpy).toHaveBeenCalledWith('/api/share/history', expect.any(Object));
    expect(out.history).toHaveLength(1);
    expect(out.history[0].template_id).toBe('weekly-recap');
  });

  it('appendHistory POSTs body as JSON', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        recipe_id: 'abc123',
        panel: 'weekly',
        template_id: 'weekly-recap',
        options: defaults(),
        format: 'md',
        destination: 'copy',
        exported_at: '2026-05-11T10:00:00Z',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    const rec = await appendHistory({
      panel: 'weekly',
      template_id: 'weekly-recap',
      options: defaults(),
      format: 'md',
      destination: 'copy',
    });
    expect(fetchSpy).toHaveBeenCalledWith('/api/share/history', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
    }));
    const call = fetchSpy.mock.calls[0];
    const opts = call[1] as RequestInit;
    expect(JSON.parse(opts.body as string).destination).toBe('copy');
    expect(rec.recipe_id).toBe('abc123');
  });

  it('clearHistory DELETEs /api/share/history', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    await clearHistory();
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/share/history',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('clearHistory surfaces server errors as ShareApiError', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'boom' }), { status: 500 }),
    );
    await expect(clearHistory()).rejects.toBeInstanceOf(ShareApiError);
  });
});
