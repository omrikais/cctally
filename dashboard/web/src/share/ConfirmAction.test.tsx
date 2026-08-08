// #503 S3 §2 — the confirmation primitive.
//
// The per-site wiring is covered in each site's own suite; this file pins
// the contract every site depends on: nothing commits on the first click,
// focus lands on Confirm, the prompt is referenced by aria-describedby and
// announced, Escape cancels, and — the reason this is a host-level hook —
// only ONE confirmation can be open per host at a time.
import { useState } from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ConfirmAction, useConfirmHost, canTakeFocus, CONFIRM_LAYER,
} from './ConfirmAction';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  registerKeymap,
  registeredBindings,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
import { _resetForTests as _resetStore } from '../store/store';

function Host({ onA, onB }: { onA?: () => void; onB?: () => void }) {
  const confirm = useConfirmHost();
  return (
    <div>
      <h2 id="fallback" tabIndex={-1}>Heading</h2>
      <button type="button" id="trigger-a" onClick={() => confirm.arm('a')}>
        Delete A
      </button>
      <ConfirmAction
        id="a"
        host={confirm}
        prompt='Delete "A"?'
        confirmLabel="Delete"
        onConfirm={() => {
          onA?.();
          confirm.close(() => document.getElementById('fallback'));
        }}
      />
      <button type="button" id="trigger-b" onClick={() => confirm.arm('b')}>
        Delete B
      </button>
      <ConfirmAction
        id="b"
        host={confirm}
        prompt='Delete "B"?'
        confirmLabel="Delete"
        onConfirm={() => { onB?.(); confirm.close(); }}
      />
    </div>
  );
}

beforeEach(() => {
  _resetStore();
  _resetKeymap();
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
  _resetKeymap();
  vi.restoreAllMocks();
});

describe('<ConfirmAction>', () => {
  it('renders nothing until its host arms it', () => {
    render(<Host />);
    expect(screen.queryByText('Delete "A"?')).not.toBeInTheDocument();
  });

  it('the first click commits nothing', () => {
    const onA = vi.fn();
    render(<Host onA={onA} />);
    fireEvent.click(screen.getByText('Delete A'));
    expect(onA).not.toHaveBeenCalled();
    expect(screen.getByText('Delete "A"?')).toBeInTheDocument();
  });

  it('Confirm commits', async () => {
    const onA = vi.fn();
    render(<Host onA={onA} />);
    fireEvent.click(screen.getByText('Delete A'));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(onA).toHaveBeenCalledTimes(1);
  });

  it('Cancel commits nothing and restores the initiating control', async () => {
    const onA = vi.fn();
    render(<Host onA={onA} />);
    const trigger = screen.getByText('Delete A');
    trigger.focus();
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    expect(onA).not.toHaveBeenCalled();
    expect(screen.queryByText('Delete "A"?')).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });

  it('Escape commits nothing and restores the initiating control', async () => {
    const onA = vi.fn();
    render(<Host onA={onA} />);
    const trigger = screen.getByText('Delete A');
    trigger.focus();
    fireEvent.click(trigger);
    await screen.findByText('Delete "A"?');
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByText('Delete "A"?')).not.toBeInTheDocument());
    expect(onA).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(trigger);
  });

  it('moves focus to Confirm and describes it with the prompt', async () => {
    render(<Host />);
    fireEvent.click(screen.getByText('Delete A'));
    const confirmBtn = await screen.findByRole('button', { name: 'Delete' });
    await waitFor(() => expect(document.activeElement).toBe(confirmBtn));
    const promptId = confirmBtn.getAttribute('aria-describedby');
    expect(promptId).toBeTruthy();
    const prompt = document.getElementById(promptId as string);
    expect(prompt).toHaveTextContent('Delete "A"?');
    // A polite live region, so the prompt is also announced on insertion.
    expect(prompt).toHaveAttribute('role', 'status');
  });

  it('sends focus to the supplied fallback after a confirm', async () => {
    render(<Host />);
    fireEvent.click(screen.getByText('Delete A'));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    await waitFor(() =>
      expect(document.activeElement).toBe(document.getElementById('fallback')));
  });

  it('opens only ONE confirmation per host', async () => {
    render(<Host />);
    fireEvent.click(screen.getByText('Delete A'));
    await screen.findByText('Delete "A"?');
    fireEvent.click(screen.getByText('Delete B'));
    await screen.findByText('Delete "B"?');
    // Arming the second closed the first — otherwise Escape would resolve
    // by registration order rather than by what the user is looking at.
    expect(screen.queryByText('Delete "A"?')).not.toBeInTheDocument();
  });

  it('Escape cancels the ACTIVE confirmation, not an earlier one', async () => {
    const onA = vi.fn();
    const onB = vi.fn();
    render(<Host onA={onA} onB={onB} />);
    fireEvent.click(screen.getByText('Delete A'));
    await screen.findByText('Delete "A"?');
    fireEvent.click(screen.getByText('Delete B'));
    await screen.findByText('Delete "B"?');
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByText('Delete "B"?')).not.toBeInTheDocument());
    expect(onA).not.toHaveBeenCalled();
    expect(onB).not.toHaveBeenCalled();
  });

  it('registers exactly one Escape binding, above the composer layer',
    async () => {
      render(<Host />);
      fireEvent.click(screen.getByText('Delete A'));
      await screen.findByText('Delete "A"?');
      const escapes = registeredBindings().filter((b) => b.key === 'Escape');
      expect(escapes).toHaveLength(1);
      expect(escapes[0].scope).toBe('overlay');
      expect(CONFIRM_LAYER).toBeGreaterThan(210);
    });

  it('wins Escape over a lower-layer overlay binding while armed',
    async () => {
      const hostEscape = vi.fn();
      registerKeymap([{
        key: 'Escape', scope: 'overlay', layer: 210, action: hostEscape,
      }]);
      const onA = vi.fn();
      render(<Host onA={onA} />);
      fireEvent.click(screen.getByText('Delete A'));
      await screen.findByText('Delete "A"?');
      fireEvent.keyDown(document, { key: 'Escape' });
      await waitFor(() =>
        expect(screen.queryByText('Delete "A"?')).not.toBeInTheDocument());
      expect(hostEscape).not.toHaveBeenCalled();
      // …and the host's own Escape is untouched once nothing is armed.
      fireEvent.keyDown(document, { key: 'Escape' });
      expect(hostEscape).toHaveBeenCalledTimes(1);
    });
});

/**
 * Records the target's `disabled` state at the MOMENT `focus()` is called.
 *
 * Asserting `document.activeElement` afterwards is a weaker check, because
 * where focus lands depends on whether React committed the re-enable before
 * the restore ran. That answer differed between jsdom and Chrome, so the
 * overwrite-rename site passed its `activeElement` assertion while a real
 * browser showed focus on <body>. The assertion has to be about the call.
 */
function installFocusSpy(): Array<{ el: HTMLElement; disabled: boolean }> {
  const calls: Array<{ el: HTMLElement; disabled: boolean }> = [];
  const real = HTMLElement.prototype.focus;
  vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(
    function focusRecorder(this: HTMLElement, options?: FocusOptions) {
      calls.push({
        el: this,
        disabled: (this as Partial<HTMLButtonElement>).disabled === true,
      });
      real.call(this, options);
    },
  );
  return calls;
}

/**
 * Models every destructive site in the share surface: the control the
 * confirmation hands focus back to is DISABLED while the operation runs,
 * and it is re-enabled by the site's own state update.
 *
 * `beforeClose` is the contract `close()` documents — clear the busy flag,
 * then close. `afterClose` violates it, and stands in for a future caller
 * that re-enables its controls a tick later.
 */
function BusyHost({ clearBusy }: { clearBusy: 'beforeClose' | 'afterClose' }) {
  const confirm = useConfirmHost();
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    const target = document.getElementById('next');
    await Promise.resolve();          // the in-flight operation
    if (clearBusy === 'beforeClose') {
      setBusy(false);
      confirm.close(() => target);
    } else {
      confirm.close(() => target);
      setTimeout(() => setBusy(false), 0);
    }
  }

  return (
    <div>
      <button type="button" id="trigger" onClick={() => confirm.arm('x')}>
        Delete
      </button>
      <ConfirmAction
        id="x"
        host={confirm}
        prompt="Delete?"
        confirmLabel="Confirm"
        onConfirm={() => { void run(); }}
      />
      <button type="button" id="next" disabled={busy}>Next</button>
    </div>
  );
}

async function confirmAndSettle(): Promise<HTMLButtonElement> {
  fireEvent.click(screen.getByText('Delete'));
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm' }));
  const next = document.getElementById('next') as HTMLButtonElement;
  await waitFor(() => expect(document.activeElement).toBe(next));
  return next;
}

describe('useConfirmHost focus restoration', () => {
  it('canTakeFocus separates a focusable control from one that only looks it',
    () => {
      render(
        <>
          <button type="button" id="on">On</button>
          <button type="button" id="off" disabled>Off</button>
        </>,
      );
      expect(canTakeFocus(document.getElementById('on'))).toBe(true);
      // Attached, has focus(), and still absorbs the call.
      expect(canTakeFocus(document.getElementById('off'))).toBe(false);
      expect(canTakeFocus(document.createElement('button'))).toBe(false);
      expect(canTakeFocus(null)).toBe(false);
    });

  it('never calls focus() while the restore target is still disabled',
    async () => {
      const calls = installFocusSpy();
      render(<BusyHost clearBusy="beforeClose" />);
      const next = await confirmAndSettle();
      expect(next.disabled).toBe(false);
      // THE property. The restore used to run inside `close()`, before React
      // committed the re-enable, so `focus()` was called on a disabled
      // button and focus stayed where it was.
      expect(calls.filter((c) => c.el === next).map((c) => c.disabled))
        .toEqual([false]);
    });

  it('waits for a target the site re-enables a tick after the close',
    async () => {
      const calls = installFocusSpy();
      render(<BusyHost clearBusy="afterClose" />);
      const next = await confirmAndSettle();
      expect(next.disabled).toBe(false);
      expect(calls.filter((c) => c.el === next).map((c) => c.disabled))
        .toEqual([false]);
    });
});
