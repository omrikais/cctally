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
    fireEvent.click(screen.getByRole('menuitem', { name: /whole transcript.*copy/i }));

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
    fireEvent.click(screen.getByRole('menuitem', { name: /replay recipe.*download/i }));

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

  it('#238 R3 — dismisses on outside pointerdown without refocusing the trigger', () => {
    render(
      <div>
        <ExportMenu sessionId="s1" title="t" />
        <button data-testid="outside">outside</button>
      </div>,
    );
    fireEvent.click(screen.getByRole('button', { name: /export/i }));
    expect(screen.getByRole('button', { name: /export/i })).toHaveAttribute('aria-expanded', 'true');
    fireEvent.pointerDown(screen.getByTestId('outside'));
    expect(screen.getByRole('button', { name: /export/i })).toHaveAttribute('aria-expanded', 'false');
    // Silent dismiss: focus must NOT have been forced back onto the trigger.
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: /export/i }));
  });

  it('#238 R3 — a pointerdown INSIDE the menu does not dismiss it', () => {
    render(<ExportMenu sessionId="s1" title="t" />);
    fireEvent.click(screen.getByRole('button', { name: /export/i }));
    fireEvent.pointerDown(screen.getByRole('menuitem', { name: /whole transcript.*copy/i }));
    expect(screen.getByRole('button', { name: /export/i })).toHaveAttribute('aria-expanded', 'true');
  });

  // #224 — APG menu keyboard pattern: the action buttons are role="menuitem"
  // with roving tabindex; Arrow/Home/End move focus, focus enters the menu on
  // open.
  describe('keyboard (APG menu pattern)', () => {
    const open = () => {
      render(<ExportMenu sessionId="s1" title="t" />);
      fireEvent.click(screen.getByRole('button', { name: /export/i }));
    };

    it('moves focus to the first menuitem on open', () => {
      open();
      expect(document.activeElement).toBe(
        screen.getByRole('menuitem', { name: /whole transcript.*copy/i }),
      );
    });

    it('roving tabindex: only the active menuitem is tabbable', () => {
      open();
      const first = screen.getByRole('menuitem', { name: /whole transcript.*copy/i });
      const second = screen.getByRole('menuitem', { name: /whole transcript.*download/i });
      expect(first.getAttribute('tabindex')).toBe('0');
      expect(second.getAttribute('tabindex')).toBe('-1');
      fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' });
      expect(first.getAttribute('tabindex')).toBe('-1');
      expect(second.getAttribute('tabindex')).toBe('0');
    });

    it('ArrowDown advances roving focus to the next menuitem', () => {
      open();
      fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' });
      expect(document.activeElement).toBe(
        screen.getByRole('menuitem', { name: /whole transcript.*download/i }),
      );
    });

    it('ArrowUp from the first menuitem wraps to the last', () => {
      open();
      fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowUp' });
      expect(document.activeElement).toBe(
        screen.getByRole('menuitem', { name: /replay recipe.*download/i }),
      );
    });

    it('End jumps to the last menuitem, Home back to the first', () => {
      open();
      const menu = screen.getByRole('menu');
      fireEvent.keyDown(menu, { key: 'End' });
      expect(document.activeElement).toBe(
        screen.getByRole('menuitem', { name: /replay recipe.*download/i }),
      );
      fireEvent.keyDown(menu, { key: 'Home' });
      expect(document.activeElement).toBe(
        screen.getByRole('menuitem', { name: /whole transcript.*copy/i }),
      );
    });
  });

  // #281 S4 — Anonymize mode.
  it('anon mode ON: copy fetches &anonymize=1 and the menu shows the anon note', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => '# T' });
    vi.stubGlobal('fetch', fetchMock);
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
    render(<ExportMenu sessionId="s1" title="My Sess" anonMode />);
    fireEvent.click(screen.getByRole('button', { name: /export/i }));
    expect(screen.getByText(/anonymized/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('menuitem', { name: /whole transcript.*copy/i }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/conversation/s1/export?scope=all&anonymize=1'),
      ),
    );
  });

  it('anon mode ON: download filename gains an -anon suffix', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => '# T' });
    vi.stubGlobal('fetch', fetchMock);
    let downloadName = '';
    const realCreate = document.createElement.bind(document);
    const createSpy = vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = realCreate(tag) as HTMLElement;
      if (tag === 'a') {
        (el as HTMLAnchorElement).click = () => {
          downloadName = (el as HTMLAnchorElement).download;
        };
      }
      return el;
    });
    vi.stubGlobal('URL', { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} });
    render(<ExportMenu sessionId="s1" title="My Sess" anonMode />);
    fireEvent.click(screen.getByRole('button', { name: /export/i }));
    fireEvent.click(screen.getByRole('menuitem', { name: /replay recipe.*download/i }));
    await waitFor(() => expect(downloadName).toBe('My-Sess-recipe-anon.md'));
    createSpy.mockRestore();
  });

  it('anon mode OFF: URL has no anonymize param, filename has no -anon (byte-identical)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => '# T' });
    vi.stubGlobal('fetch', fetchMock);
    let downloadName = '';
    const realCreate = document.createElement.bind(document);
    const createSpy = vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = realCreate(tag) as HTMLElement;
      if (tag === 'a') {
        (el as HTMLAnchorElement).click = () => {
          downloadName = (el as HTMLAnchorElement).download;
        };
      }
      return el;
    });
    vi.stubGlobal('URL', { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} });
    render(<ExportMenu sessionId="s1" title="My Sess" />);
    fireEvent.click(screen.getByRole('button', { name: /export/i }));
    expect(screen.queryByText(/anonymized/i)).toBeNull();
    fireEvent.click(screen.getByRole('menuitem', { name: /replay recipe.*download/i }));
    await waitFor(() => expect(downloadName).toBe('My-Sess-recipe.md'));
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).not.toContain('anonymize');
    createSpy.mockRestore();
  });

  it('slugifyTitle strips non-ascii/control and caps, falling back to session id', () => {
    expect(slugifyTitle('Hello World! 你好', 'abc123def456')).toBe('Hello-World');
    expect(slugifyTitle('', 'abc123def456789')).toBe('abc123def456');
    expect(slugifyTitle('   ', 'sessionXYZ0001')).toBe('sessionXYZ00');
  });
});

// silence unused-import lints under noUnusedLocals
void act;
