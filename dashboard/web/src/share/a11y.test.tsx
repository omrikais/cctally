// Task M4.4 — a11y / reduced-motion / focus-restoration audit
// regressions (spec §12.4-§12.8). Pins the invariants enumerated in the
// plan task so a future drop of `aria-modal`, the `aria-labelledby`
// link, or the trigger-element focus capture is caught here rather than
// at the screen-reader / keyboard-user end.
//
// What we assert per modal surface:
//   - `<ShareModal>`     : role=dialog + aria-modal + aria-labelledby
//                          → already covered in ShareModalRoot.test.tsx,
//                          repeated here as part of the audit's single
//                          source of truth.
//   - `<ComposerModal>`  : role=dialog + aria-modal=true +
//                          aria-labelledby pointing at the visible
//                          title; focus restored to the opener element
//                          on close (with document.body fallback when
//                          the opener has been unmounted).
//   - `<ManagePresetsModal>`: role=dialog + aria-modal=true +
//                          aria-labelledby pointing at the visible
//                          title; focus restored to the opener.
//
// We drive the store directly (BASKET_HYDRATE / openComposer / open
// prop) instead of clicking through the GUI — the focus-restoration
// contract is "captured opener element gets focus back," and we want
// the test to be robust to surface-area changes (Customize…
// affordance, B keymap, BasketChip click are all valid openers).
import {
  render, screen, act, cleanup, waitFor, within, fireEvent,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ComposerModal } from './ComposerModal';
import { ManagePresetsModal } from './ManagePresetsModal';
import { ShareModalRoot } from './ShareModalRoot';
import { SHARE_PRESETS_TRIGGER_ID } from './PresetDropdown';
import { _resetForTests, dispatch } from '../store/store';
import { openShareModal, closeShareModal, openComposer, closeComposer } from '../store/shareSlice';
import {
  installGlobalKeydown, registeredBindings, _resetForTests as _resetKeymap,
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

beforeEach(() => {
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  // matchMedia stub — composer reads useIsMobile() (matchMedia) on
  // mount; reduced-motion subscribers in this surface also go through
  // matchMedia. Stubbed to "no match" so every media query resolves
  // false unless a test overrides it.
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {},
    dispatchEvent: () => false,
  }));
  // fetch stub for ShareModal templates + presets. Switches on URL.
  vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => {
    if (typeof url === 'string' && url.includes('/api/share/presets')) {
      return Promise.resolve(new Response(
        JSON.stringify({ presets: {} }),
        { status: 200 },
      ));
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({
        panel: 'weekly',
        templates: [{
          id: 'weekly-recap',
          label: 'Recap',
          description: 'Text + tiny chart',
          default_options: { format: 'md', theme: 'light' },
        }],
      }),
    });
  }));
});

afterEach(() => {
  cleanup();
  _resetKeymap();
  vi.restoreAllMocks();
});

describe('share-v2 a11y attributes', () => {
  it('<ShareModal> dialog carries role=dialog + aria-modal + aria-labelledby', async () => {
    render(<ShareModalRoot />);
    await act(async () => {
      dispatch(openShareModal('weekly', null));
    });
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    const labelledby = dialog.getAttribute('aria-labelledby');
    expect(labelledby).toBeTruthy();
    // The referenced id must resolve to a real element with non-empty
    // text content — otherwise screen readers announce "dialog" with
    // no name.
    const labelEl = labelledby ? document.getElementById(labelledby) : null;
    expect(labelEl).not.toBeNull();
    expect(labelEl?.textContent?.trim().length ?? 0).toBeGreaterThan(0);
  });

  it('<ComposerModal> dialog carries role=dialog + aria-modal + aria-labelledby', async () => {
    dispatch({
      type: 'BASKET_HYDRATE',
      items: [{
        id: '1', panel: 'weekly', template_id: 'weekly-recap',
        options: defaultOpts(), added_at: '2026-05-12T09:00:00Z',
        data_digest_at_add: 'sha256:abc', kernel_version: 1,
        label_hint: 'Weekly recap', source: 'claude' as const,
      } satisfies BasketItem],
    });
    dispatch(openComposer());
    render(<ComposerModal />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    const labelledby = dialog.getAttribute('aria-labelledby');
    expect(labelledby).toBeTruthy();
    const labelEl = labelledby ? document.getElementById(labelledby) : null;
    expect(labelEl).not.toBeNull();
    expect(labelEl?.textContent?.trim().length ?? 0).toBeGreaterThan(0);
  });

  it('<ComposerModal> empty-state still carries the aria attributes', async () => {
    // Hydrate empty basket (no BASKET_HYDRATE call). openComposer +
    // mount renders the empty-state branch which has its own dialog
    // root; same a11y contract applies.
    dispatch(openComposer());
    render(<ComposerModal />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    const labelledby = dialog.getAttribute('aria-labelledby');
    expect(labelledby).toBeTruthy();
    const labelEl = labelledby ? document.getElementById(labelledby) : null;
    expect(labelEl).not.toBeNull();
    expect(labelEl?.textContent?.trim().length ?? 0).toBeGreaterThan(0);
  });

  it('<ManagePresetsModal> dialog carries role=dialog + aria-modal + aria-labelledby', () => {
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    const labelledby = dialog.getAttribute('aria-labelledby');
    expect(labelledby).toBeTruthy();
    const labelEl = labelledby ? document.getElementById(labelledby) : null;
    expect(labelEl).not.toBeNull();
    expect(labelEl?.textContent?.trim().length ?? 0).toBeGreaterThan(0);
  });
});

describe('share-v2 focus restoration', () => {
  it('<ComposerModal> restores focus to the opener element on close', async () => {
    const opener = document.createElement('button');
    opener.id = 'composer-opener';
    opener.textContent = 'Open composer';
    document.body.appendChild(opener);
    // The opener is the active element at the moment the modal is
    // opened — mirrors a real user clicking the BasketChip / pressing
    // `B` from a focused control.
    opener.focus();
    expect(document.activeElement).toBe(opener);
    try {
      dispatch(openComposer());
      render(<ComposerModal />);
      // Sanity: composer dialog mounted.
      expect(screen.getByRole('dialog')).toBeInTheDocument();
      // Simulate the modal taking focus while open (e.g. the user
      // tabbed into an internal control). Without an explicit restore
      // step, blurring + closing would leave activeElement on body, not
      // on the opener.
      (document.activeElement as HTMLElement | null)?.blur?.();
      const stealFocusTarget = document.createElement('button');
      stealFocusTarget.id = 'inside-composer-focus-sink';
      document.body.appendChild(stealFocusTarget);
      stealFocusTarget.focus();
      expect(document.activeElement).toBe(stealFocusTarget);
      // Close the composer — focus must return to the opener.
      await act(async () => {
        dispatch(closeComposer());
      });
      stealFocusTarget.remove();
      expect(document.activeElement).toBe(opener);
    } finally {
      opener.remove();
    }
  });

  it('<ComposerModal> falls back to document.body when opener is unmounted', async () => {
    const opener = document.createElement('button');
    opener.id = 'composer-opener-transient';
    opener.textContent = 'Open composer';
    document.body.appendChild(opener);
    opener.focus();
    dispatch(openComposer());
    render(<ComposerModal />);
    // Simulate the opener being torn out of the DOM while the composer
    // is open (e.g. the panel rerendered and the BasketChip is no
    // longer mounted in that slot).
    opener.remove();
    await act(async () => {
      dispatch(closeComposer());
    });
    // We don't assert a specific element; just that the previous
    // activeElement (the now-detached opener) is no longer focused and
    // the implementation hasn't crashed.
    expect(document.activeElement).not.toBe(opener);
  });

  it('<ManagePresetsModal> restores focus to the opener element on close', async () => {
    const opener = document.createElement('button');
    opener.id = 'manage-opener';
    opener.textContent = 'Manage presets';
    document.body.appendChild(opener);
    opener.focus();
    try {
      const { rerender } = render(
        <ManagePresetsModal open={true} onClose={() => {}} />,
      );
      // Sanity: dialog mounted.
      expect(screen.getByRole('dialog')).toBeInTheDocument();
      // Move focus off the opener to validate the restore path.
      const stealFocusTarget = document.createElement('button');
      stealFocusTarget.id = 'inside-manage-focus-sink';
      document.body.appendChild(stealFocusTarget);
      stealFocusTarget.focus();
      expect(document.activeElement).toBe(stealFocusTarget);
      // Close it by switching the `open` prop.
      rerender(<ManagePresetsModal open={false} onClose={() => {}} />);
      // Wait one tick for the useEffect cleanup to run.
      await waitFor(() => {
        expect(document.activeElement).toBe(opener);
      });
      stealFocusTarget.remove();
    } finally {
      opener.remove();
    }
  });

  it('<ShareModalRoot> focus restore falls back to document.body when the triggerId element is gone', async () => {
    // Stage the opener, open the share modal pointing at its id, then
    // remove the opener before dispatching close. The fallback path
    // must blur whatever currently has focus (rather than no-oping and
    // leaving focus on a stale internal control inside the now-closed
    // modal).
    const trigger = document.createElement('button');
    trigger.id = 'transient-share-trigger';
    trigger.textContent = 'Share';
    document.body.appendChild(trigger);
    render(<ShareModalRoot />);
    await act(async () => {
      dispatch(openShareModal('weekly', 'transient-share-trigger'));
    });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // Tear down the trigger while the modal is open.
    trigger.remove();
    // Park focus on a stand-in for an internal control (e.g. the
    // ActionBar's copy button). Without the body-focus fallback, this
    // would still be the active element after the modal closes —
    // including after the parent container that hosted it unmounts.
    const internalControl = document.createElement('button');
    internalControl.id = 'share-internal-control';
    document.body.appendChild(internalControl);
    internalControl.focus();
    expect(document.activeElement).toBe(internalControl);
    await act(async () => {
      dispatch(closeShareModal());
    });
    // The fallback must have moved focus off the internal control. We
    // accept either body or the unmount-default — anything except the
    // detached trigger or the parked internal control.
    expect(document.activeElement).not.toBe(trigger);
    expect(document.activeElement).not.toBe(internalControl);
    internalControl.remove();
  });
});

// #503 S4 §3.1 / #531 item 2 — Manage presets claimed `role="dialog"
// aria-modal="true"` while computing to `position: static; z-index: auto`: an
// in-flow block at the bottom of the share modal that trapped nothing, owned
// Escape only because ShareModal gated itself out, and restored focus to an
// element that had already left the DOM.
//
// The presets fixture below is NOT decoration. With no saved presets the
// dialog holds exactly one focusable (its close button), and a one-element
// focusable set makes the Tab-wrap assertion vacuous — first and last are the
// same node, so a trap and no trap look identical.
const MANAGE_FIXTURE_PRESETS = {
  weekly: {
    Alpha: { template_id: 'weekly-recap', options: {}, saved_at: '2026-05-01T00:00:00Z' },
    Beta: { template_id: 'weekly-recap', options: {}, saved_at: '2026-05-02T00:00:00Z' },
  },
};

function stubShareFetchWithPresets(): void {
  vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => {
    if (typeof url === 'string' && url.includes('/api/share/history')) {
      return Promise.resolve(new Response(
        JSON.stringify({ history: [] }), { status: 200 },
      ));
    }
    if (typeof url === 'string' && url.includes('/api/share/presets')) {
      return Promise.resolve(new Response(
        JSON.stringify({ presets: MANAGE_FIXTURE_PRESETS }), { status: 200 },
      ));
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({
        panel: 'weekly',
        templates: [{
          id: 'weekly-recap', label: 'Recap',
          description: 'Text + tiny chart',
          default_options: { format: 'md', theme: 'light' },
        }],
      }),
    });
  }));
}

async function renderShareModalWithManageOpen() {
  stubShareFetchWithPresets();
  render(<ShareModalRoot />);
  await act(async () => {
    dispatch(openShareModal('weekly', null));
  });
  // Open the preset dropdown, then its "Manage presets…" footer item — the
  // real path, which is what makes the dropdown (and the menuitem the old
  // restore effect captured) unmount as Manage opens.
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole('menuitem', { name: /manage presets/i }));
  });
  const dialog = await screen.findByRole('dialog', { name: /manage presets/i });
  await waitFor(() => {
    expect(within(dialog).getAllByRole('button', { name: /^rename$/i }).length)
      .toBeGreaterThan(1);
  });
  const closeManage = async () => {
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: /close/i }));
    });
  };
  return { dialog, closeManage };
}

function escapeBindingCounts() {
  const all = registeredBindings().filter((b) => b.key === 'Escape');
  const overlay = all.filter((b) => b.scope === 'overlay');
  return {
    overlay: overlay.length,
    modal: all.filter((b) => b.scope === 'modal').length,
    layers: overlay.map((b) => b.layer ?? 0).sort((a, b) => a - b),
  };
}

describe('share-v2 Manage presets is a real modal layer (#503 S4 §3.1)', () => {
  it('<ManagePresetsModal> contains Tab while open', async () => {
    const { dialog } = await renderShareModalWithManageOpen();
    const focusables = within(dialog).getAllByRole('button');
    expect(focusables.length).toBeGreaterThan(1);   // guards the wrap assertion
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    act(() => { last.focus(); });
    fireEvent.keyDown(document, { key: 'Tab' });
    // Containment, asserted as the WRAP rather than as mere presence: without
    // the dialog's own trap the share modal's trap owns this keydown and sends
    // focus to ITS first focusable, out in the template gallery. Asserting only
    // `dialog.contains(activeElement)` would also pass in the case where
    // nothing moved focus at all.
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toBe(first);
  });

  it('restores focus to the presets trigger, which outlives the dropdown', async () => {
    const { closeManage } = await renderShareModalWithManageOpen();
    await closeManage();
    // The hand-rolled effect this replaced captured `document.activeElement`
    // at open — the "Manage presets…" menuitem, which unmounts as Manage
    // opens. It therefore always fell into its blur-and-focus-body fallback.
    await waitFor(() => {
      expect(document.activeElement)
        .toBe(document.getElementById(SHARE_PRESETS_TRIGGER_ID));
    });
    expect(document.getElementById(SHARE_PRESETS_TRIGGER_ID)).not.toBeNull();
  });

  it('<ManagePresetsModal> owns Escape at overlay scope, not modal scope', async () => {
    stubShareFetchWithPresets();
    render(<ShareModalRoot />);
    await act(async () => {
      dispatch(openShareModal('weekly', null));
    });
    const before = escapeBindingCounts();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('menuitem', { name: /manage presets/i }));
    });
    await screen.findByRole('dialog', { name: /manage presets/i });
    const after = escapeBindingCounts();
    // A bare `overlay Escape count > 0` would pass before the fix, because
    // ShareModal and PresetDropdown already register two. The discriminator is
    // that opening Manage ADDS an overlay binding and adds NO modal-scope one:
    // before the fix it was the other way round.
    expect(after.overlay).toBe(before.overlay + 1);
    expect(after.modal).toBe(0);
    // The NUMBER, not just the scope. 208 is hardcoded rather than imported
    // from ManagePresetsModal on purpose: a tripwire that reads the constant
    // it guards moves with the code and can never fail. The scope assertions
    // above leave 208 as prose — they pass just as well at 205, which loses
    // Escape to the preset dropdown, or at 220, which takes it from an armed
    // rename confirmation.
    expect(after.layers).toEqual([...before.layers, 208].sort((a, b) => a - b));
  });
});
