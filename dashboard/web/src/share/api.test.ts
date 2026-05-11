import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fetchTemplates, renderShare } from './api';
import type { ShareOptions } from './types';

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('share api', () => {
  it('fetchTemplates returns the parsed body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        panel: 'weekly',
        templates: [{ id: 'weekly-recap', label: 'Recap', description: '...', default_options: {} }],
      }),
    }));
    const result = await fetchTemplates('weekly');
    expect(result.templates[0].id).toBe('weekly-recap');
  });

  it('renderShare throws ShareApiError on 400', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 400,
      json: async () => ({ error: 'unknown template_id', field: 'template_id' }),
    }));
    await expect(renderShare({
      panel: 'weekly', template_id: 'bogus',
      options: { format: 'md', theme: 'light' } as ShareOptions,
    })).rejects.toMatchObject({ status: 400, code: undefined });
  });
});
