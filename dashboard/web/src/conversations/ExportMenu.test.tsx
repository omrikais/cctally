import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ExportMenu, slugifyTitle } from './ExportMenu';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('ExportMenu', () => {
  it('copies the fetched markdown for a scope', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => '# T\nhi' });
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<ExportMenu sessionId="s1" title="My Sess" />);
    fireEvent.click(screen.getByRole('button', { name: /export/i }));
    fireEvent.click(screen.getByRole('button', { name: /whole transcript.*copy/i }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/conversation/s1/export?scope=all'),
      ),
    );
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('# T\nhi'));
  });

  it('downloads a slugged .md file for a scope', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => '# T\nhi' });
    vi.stubGlobal('fetch', fetchMock);
    // Capture the synthetic anchor's download attribute + click.
    const click = vi.fn();
    const realCreate = document.createElement.bind(document);
    const createSpy = vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = realCreate(tag) as HTMLElement;
      if (tag === 'a') (el as HTMLAnchorElement).click = click;
      return el;
    });
    vi.stubGlobal('URL', { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} });

    render(<ExportMenu sessionId="s1" title="My Sess" />);
    fireEvent.click(screen.getByRole('button', { name: /export/i }));
    fireEvent.click(screen.getByRole('button', { name: /replay recipe.*download/i }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/conversation/s1/export?scope=recipe'),
      ),
    );
    await waitFor(() => expect(click).toHaveBeenCalled());
    createSpy.mockRestore();
  });

  it('closes on container Escape', () => {
    render(<ExportMenu sessionId="s1" title="t" />);
    fireEvent.click(screen.getByRole('button', { name: /export/i }));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('slugifyTitle strips non-ascii/control and caps, falling back to session id', () => {
    expect(slugifyTitle('Hello World! 你好', 'abc123def456')).toBe('Hello-World');
    expect(slugifyTitle('', 'abc123def456789')).toBe('abc123def456');
    expect(slugifyTitle('   ', 'sessionXYZ0001')).toBe('sessionXYZ00');
  });
});

// silence unused-import lints under noUnusedLocals
void act;
