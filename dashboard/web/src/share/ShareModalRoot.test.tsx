// ShareModalRoot — plan §M1.11 contract:
//   - Renders nothing when state.shareModal === null.
//   - Renders <ShareModal> wrapped in .share-overlay when non-null.
//   - On close, focus returns to the element identified by triggerId.
//
// We mock the share API so the gallery fetch resolves synchronously
// (avoids spinning the test on a network round-trip) and the modal can
// reach its rendered state.
import { render, screen, act, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ShareModalRoot } from './ShareModalRoot';
import { dispatch, _resetForTests } from '../store/store';
import { openShareModal, closeShareModal } from '../store/shareSlice';
import { installGlobalKeydown, _resetForTests as _resetKeymap } from '../store/keymap';
import { Modal } from '../modals/Modal';

beforeEach(() => {
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      panel: 'weekly',
      templates: [
        {
          id: 'weekly-recap',
          label: 'Recap',
          description: 'Text + tiny chart',
          default_options: { format: 'md', theme: 'light' },
        },
      ],
    }),
  }));
});

// Convenience for the nested-Esc regression test: a single fetch stub
// that answers both the share-modal templates fetch AND the presets
// fetch driven by <PresetDropdown>. Switches on URL substring.
function stubFetchForNestedModal() {
  vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => {
    if (typeof url === 'string' && url.includes('/api/share/presets')) {
      return Promise.resolve(new Response(
        JSON.stringify({ presets: {} }),
        { status: 200 },
      ));
    }
    // Default: /api/share/templates response.
    return Promise.resolve({
      ok: true,
      json: async () => ({
        panel: 'weekly',
        templates: [
          {
            id: 'weekly-recap',
            label: 'Recap',
            description: 'Text + tiny chart',
            default_options: { format: 'md', theme: 'light' },
          },
        ],
      }),
    });
  }));
}

afterEach(() => {
  cleanup();
  _resetKeymap();
  vi.restoreAllMocks();
});

describe('<ShareModalRoot>', () => {
  it('renders nothing when shareModal slot is null', () => {
    const { container } = render(<ShareModalRoot />);
    expect(container.querySelector('.share-overlay')).toBeNull();
  });

  it('mounts the share modal when slot is set', async () => {
    render(<ShareModalRoot />);
    await act(async () => {
      dispatch(openShareModal('weekly', null));
    });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toHaveAttribute('aria-modal', 'true');
    expect(
      screen.getByRole('heading', { name: /share weekly report/i }),
    ).toBeInTheDocument();
  });

  it('restores focus to the trigger element by id on close', async () => {
    // Stage a stand-in for the ShareIcon button — the share modal's
    // triggerId convention is "the element id of the ShareIcon that
    // fired the dispatch."
    const trigger = document.createElement('button');
    trigger.id = 'panel-share-trigger';
    trigger.textContent = 'Share';
    document.body.appendChild(trigger);
    try {
      render(<ShareModalRoot />);
      await act(async () => {
        dispatch(openShareModal('weekly', 'panel-share-trigger'));
      });
      expect(screen.getByRole('dialog')).toBeInTheDocument();

      await act(async () => {
        dispatch(closeShareModal());
      });
      expect(document.activeElement).toBe(trigger);
    } finally {
      trigger.remove();
    }
  });

  it('closes when the overlay backdrop is clicked', async () => {
    render(<ShareModalRoot />);
    await act(async () => {
      dispatch(openShareModal('weekly', null));
    });
    const overlay = document.querySelector('.share-overlay') as HTMLElement;
    expect(overlay).not.toBeNull();
    // Click on the overlay itself (target === currentTarget triggers
    // the close path).
    await act(async () => {
      overlay.click();
    });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('closes on Escape keydown', async () => {
    render(<ShareModalRoot />);
    await act(async () => {
      dispatch(openShareModal('weekly', null));
    });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' });
    });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('Esc closes share modal first when layered atop a panel modal (spec §12.1)', async () => {
    // Render BOTH the panel-modal layer (with its own scope-modal Esc
    // handler from <Modal>) AND the share modal layer simultaneously.
    // Esc must close the topmost overlay — the share modal — not the
    // underlying panel modal. This is the regression for P1-A: under
    // the old `modal` scope, the panel modal's Esc handler (registered
    // first) would shadow the share modal's Esc handler.
    render(
      <>
        <Modal title="Panel" accentClass="accent-green">
          <div>panel body</div>
        </Modal>
        <ShareModalRoot />
      </>,
    );
    // Stage the panel modal as "open" so it actually renders chrome.
    await act(async () => {
      dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
      dispatch(openShareModal('weekly', null));
    });
    // Sanity: share modal is open (dialog count = 2: one panel modal,
    // one share modal). Both have role=dialog.
    const dialogsBefore = screen.getAllByRole('dialog');
    expect(dialogsBefore.length).toBe(2);

    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' });
    });
    // Share modal closed; panel modal still open (the panel modal's
    // dialog is the <Modal> chrome — still mounted because we did NOT
    // dispatch CLOSE_MODAL).
    const dialogsAfter = screen.getAllByRole('dialog');
    expect(dialogsAfter.length).toBe(1);
    // The remaining dialog is the panel modal (heading "Panel"), not
    // the share modal (heading "Share weekly report").
    expect(
      screen.queryByRole('heading', { name: /share weekly report/i }),
    ).toBeNull();
    expect(screen.getByRole('heading', { name: /^panel$/i })).toBeInTheDocument();
  });

  it('Esc closes nested ManagePresetsModal without closing the share modal', async () => {
    // Regression for the M2 Impl C spec-review MUST-FIX: when the nested
    // <ManagePresetsModal> is open inside <ShareModal>, pressing Esc
    // would close the entire share modal (its overlay-scope Esc binding
    // fired first via SCOPE_ORDER) instead of just the nested modal.
    //
    // The keymap dispatcher iterates bindings by scope, NOT DOM focus:
    // overlay-scope handlers always preempt modal-scope ones unless the
    // overlay binding gates itself out with `when:`. The fix wires
    // <ShareModal>'s Esc binding with `when: () => !manageOpen` so its
    // nested `modal`-scope Esc can fire while the manage modal is open.
    stubFetchForNestedModal();
    render(<ShareModalRoot />);
    await act(async () => {
      dispatch(openShareModal('weekly', null));
    });
    // Share modal rendered (its title is the dialog heading).
    expect(
      screen.getByRole('heading', { name: /share weekly report/i }),
    ).toBeInTheDocument();

    // Open the presets dropdown, then click "Manage presets…" to mount
    // the nested manage modal.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    });
    const manageItem = await screen.findByRole('menuitem', {
      name: /manage presets/i,
    });
    await act(async () => {
      fireEvent.click(manageItem);
    });
    // Sanity: nested manage modal is open (its dialog has the "Manage
    // presets" aria-label).
    await waitFor(() => {
      expect(
        screen.getByRole('dialog', { name: /manage presets/i }),
      ).toBeInTheDocument();
    });

    // Fire Esc — the nested modal must close, the share modal must NOT.
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' });
    });
    expect(
      screen.queryByRole('dialog', { name: /manage presets/i }),
    ).toBeNull();
    expect(
      screen.getByRole('heading', { name: /share weekly report/i }),
    ).toBeInTheDocument();

    // Fire Esc again — now the share modal closes (the `when:` gate
    // re-enables the overlay-scope binding once manageOpen is false).
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' });
    });
    expect(
      screen.queryByRole('heading', { name: /share weekly report/i }),
    ).toBeNull();
  });
});
