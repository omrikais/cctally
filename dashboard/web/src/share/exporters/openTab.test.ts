// #503 S3 §5 — the reserved-tab lifecycle, which is what makes a blocked or
// a vanished tab reportable instead of silently successful.
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  openDetachedTab, reserveExportTab, TAB_CLOSED_MESSAGE,
} from './openTab';

// jsdom implements neither `URL.createObjectURL` nor `URL.revokeObjectURL`,
// and `vi.restoreAllMocks()` does not undo a direct `defineProperty`, so the
// descriptors are captured and put back by hand (the ActionBar suite's
// pattern, for the same leak-into-sibling-files reason).
const propRefs: Array<[object, string, PropertyDescriptor | undefined]> = [];
function stubProperty<T extends object>(obj: T, key: string, value: unknown): void {
  propRefs.push([obj, key, Object.getOwnPropertyDescriptor(obj, key)]);
  Object.defineProperty(obj, key, { value, configurable: true });
}

interface StubWindow {
  closed: boolean;
  opener: unknown;
  location: { href: string };
  close: () => void;
}

function stubWindow(overrides: Partial<StubWindow> = {}): StubWindow {
  return {
    closed: false,
    opener: {},
    location: { href: 'about:blank' },
    close: vi.fn(),
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  while (propRefs.length > 0) {
    const [obj, key, desc] = propRefs.pop()!;
    if (desc) Object.defineProperty(obj, key, desc);
    else delete (obj as Record<string, unknown>)[key];
  }
});

describe('openDetachedTab', () => {
  it('passes no feature string and clears opener by hand', () => {
    const win = stubWindow();
    // Reproduces the specification rule a fixed-return mock cannot:
    // `window.open` returns null whenever `noopener` is present, so a
    // feature string suppresses the very handle the caller must test.
    const openSpy = vi.spyOn(window, 'open').mockImplementation(
      ((_url?: unknown, _target?: unknown, features?: unknown) =>
        (features ? null : win)) as unknown as typeof window.open,
    );

    const got = openDetachedTab();

    expect(openSpy).toHaveBeenCalledWith('', '_blank');
    expect(got).toBe(win as unknown as Window);
    expect(win.opener).toBeNull();
  });

  it('returns null when the browser blocked the tab', () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    expect(openDetachedTab()).toBeNull();
  });
});

describe('reserveExportTab', () => {
  it('navigates the reserved tab at an object URL', () => {
    const win = stubWindow();
    vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window);
    stubProperty(URL, 'createObjectURL', vi.fn().mockReturnValue('blob:stub'));

    const tab = reserveExportTab();
    tab!.navigate(new Blob(['x'], { type: 'text/html' }));

    expect(win.location.href).toBe('blob:stub');
  });

  it('throws instead of silently no-opping when the tab was closed', () => {
    // The user closed the reserved tab while the export fetch was in flight.
    // Assigning `location.href` on a closed window does nothing at all, so
    // without this the caller shows "Opened" and writes a share-history row
    // for an export that is nowhere on screen.
    const win = stubWindow({ closed: true });
    vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window);
    const createObjectURL = vi.fn().mockReturnValue('blob:stub');
    stubProperty(URL, 'createObjectURL', createObjectURL);

    const tab = reserveExportTab();
    expect(() => tab!.navigate(new Blob(['x'], { type: 'text/html' })))
      .toThrow(TAB_CLOSED_MESSAGE);
    // Checked before the URL is minted, so there is nothing left to revoke.
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(win.location.href).toBe('about:blank');
  });

  it('revokes the object URL when the tab refuses the navigation', () => {
    const location = {
      get href() { return 'about:blank'; },
      set href(_v: string) { throw new Error('refused'); },
    };
    const win = stubWindow({ location });
    vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window);
    const revokeObjectURL = vi.fn();
    stubProperty(URL, 'createObjectURL', vi.fn().mockReturnValue('blob:stub'));
    stubProperty(URL, 'revokeObjectURL', revokeObjectURL);

    const tab = reserveExportTab();
    expect(() => tab!.navigate(new Blob(['x'], { type: 'text/html' })))
      .toThrow('refused');
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:stub');
  });

  it('returns null when the reservation was blocked', () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    expect(reserveExportTab()).toBeNull();
  });
});
