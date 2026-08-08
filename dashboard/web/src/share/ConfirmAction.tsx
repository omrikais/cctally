// #503 S3 §2 — one inline confirmation primitive for every destructive
// share action.
//
// Four actions used to commit on the first click: the composer's
// `Clear all`, a preset overwrite on save, a rename onto an existing name,
// and a preset delete (which destroys a `config.json` record with no undo).
// Each now reveals an adjacent confirmation group with Confirm and Cancel,
// and focus moves to Confirm. Enter and Space are native button behaviour.
//
// THE ESCAPE CONTRACT IS WHY THIS IS A HOST-LEVEL HOOK AND NOT PER-ROW
// STATE. The keymap dispatcher resolves a same-scope, same-key tie by
// LAYER and then by registration order, not by focus (`store/keymap.ts`),
// and `ManagePresetsModal` renders one independently stateful component
// per row. If each row owned its own confirmation and its own Escape
// binding, two simultaneously open confirmations would let Escape cancel
// whichever registered first rather than the one the user is looking at.
// So `useConfirmHost` holds ONE armed id per host and registers exactly
// ONE Escape binding, whose `when` gate is "something is armed".
//
// FOCUS RESTORATION IS DEFERRED AND GUARDED, for the reason spelled out on
// `canTakeFocus` below: the confirm button unmounts on close, the site's
// restore target is a control the same site disabled while the operation ran,
// and a disabled control absorbs `focus()` without a sound.
//
// No new modal slot: `docs/share-gotchas.md` records that decision, and an
// inline group stays inside the host's existing focus and Escape ownership.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useKeymap } from '../hooks/useKeymap';

// Above ComposerModal's overlay layer 210 and the popover/dropdown 205, so
// an armed confirmation owns Escape inside any of its hosts; below Help's
// 1000, which is the deliberate escape hatch from everything.
export const CONFIRM_LAYER = 220;

/**
 * Whether `el` can actually take focus at this instant.
 *
 * Attachment used to be the whole guard, and attachment is not enough. A
 * DISABLED button is attached and exposes `focus`, and the call against it
 * does nothing at all: it does not throw, it returns nothing, and
 * `document.activeElement` does not move. Every destructive site disables
 * its row actions while the operation is in flight, so the natural restore
 * target is disabled at exactly the moment the confirmation closes.
 *
 * Asserting where focus ENDED UP is not enough to catch that, because the
 * answer depends on whether React happened to commit the re-enable before
 * the restore ran — which differed between jsdom and Chrome, so the
 * overwrite-rename site passed its vitest focus assertion while failing in
 * the browser. The suites therefore assert the target's `disabled` state at
 * the moment `focus()` is called.
 */
export function canTakeFocus(el: HTMLElement | null): el is HTMLElement {
  if (el == null || typeof el.focus !== 'function') return false;
  if (!document.contains(el)) return false;
  if ((el as Partial<HTMLButtonElement>).disabled === true) return false;
  if (typeof el.closest === 'function' && el.closest('[inert]') != null) {
    return false;
  }
  return true;
}

// How long the deferred restore keeps waiting for a target that is attached
// but cannot yet take focus. A site that clears its busy flag BEFORE calling
// `close()` is served by the first attempt, because React applies queued
// updates in call order and the restore runs after that commit. This budget
// covers a site that clears the flag a commit or a tick later, and a target
// that never becomes focusable costs a handful of idle frames.
const RESTORE_RETRY_BUDGET_MS = 250;

function onNextFrame(fn: () => void): () => void {
  if (typeof requestAnimationFrame === 'function') {
    const handle = requestAnimationFrame(() => fn());
    return () => cancelAnimationFrame(handle);
  }
  const handle = setTimeout(fn, 16);
  return () => clearTimeout(handle);
}

export interface ConfirmHost {
  /** The armed action's id, or null when nothing is awaiting confirmation. */
  armed: string | null;
  /** Arm `id`, capturing the control that initiated it for Cancel. */
  arm: (id: string) => void;
  /** Cancel: commits nothing and restores focus to the initiating control. */
  cancel: () => void;
  /**
   * Close after a COMMIT. Literal focus restoration is impossible for two of
   * the four sites — confirming a delete removes the initiating button, and a
   * rename changes the row key — so the caller supplies where focus should
   * land instead. A fallback that resolves to null or to a detached node
   * leaves focus where the browser put it rather than throwing.
   *
   * THE CALLER MUST CLEAR ITS BUSY FLAG BEFORE CALLING THIS. The restore is
   * deferred to after the commit that closes the confirmation, and React
   * applies queued updates in call order — so a `setBusy(false)` issued
   * before `close()` lands in that same commit and the target is enabled by
   * the time it is focused. A site that clears the flag afterwards is still
   * served, but only by the retry budget below rather than by construction.
   */
  close: (focusAfter?: () => HTMLElement | null) => void;
  isArmed: (id: string) => boolean;
}

export function useConfirmHost(): ConfirmHost {
  const [armed, setArmed] = useState<string | null>(null);
  const initiatorRef = useRef<HTMLElement | null>(null);
  // The restore no longer runs inside `close()`/`cancel()`. The site clears
  // its busy flag in the same batch, so at that instant the DOM still
  // carries `disabled` on every row action and the focus call is swallowed.
  // `pendingRestoreRef` carries the target across the commit boundary and
  // `restoreTick` is what guarantees an effect fires for every close, even
  // one that does not change `armed`.
  const pendingRestoreRef = useRef<HTMLElement | null>(null);
  const [restoreTick, setRestoreTick] = useState(0);
  const abortRetryRef = useRef<(() => void) | null>(null);

  const abortRestore = useCallback(() => {
    abortRetryRef.current?.();
    abortRetryRef.current = null;
    pendingRestoreRef.current = null;
  }, []);

  const scheduleRestore = useCallback((el: HTMLElement | null) => {
    abortRestore();
    pendingRestoreRef.current = el;
    setRestoreTick((n) => n + 1);
  }, [abortRestore]);

  useEffect(() => {
    if (restoreTick === 0) return;
    const el = pendingRestoreRef.current;
    pendingRestoreRef.current = null;
    if (el == null) return;
    const deadline = Date.now() + RESTORE_RETRY_BUDGET_MS;
    const attempt = () => {
      abortRetryRef.current = null;
      if (canTakeFocus(el)) {
        el.focus();
        return;
      }
      // A detached node never comes back — React rebuilt that subtree — so
      // only an attached-but-not-yet-focusable target is worth waiting for.
      if (!document.contains(el) || Date.now() >= deadline) return;
      abortRetryRef.current = onNextFrame(attempt);
    };
    attempt();
  }, [restoreTick]);

  // A pending retry must not outlive the host, or it would move focus after
  // the surface that owned it is gone.
  useEffect(() => () => { abortRetryRef.current?.(); }, []);

  const arm = useCallback((id: string) => {
    // Arming a new confirmation cancels any restore still waiting on the
    // previous one, so a late retry cannot steal focus from Confirm.
    abortRestore();
    initiatorRef.current = document.activeElement as HTMLElement | null;
    setArmed(id);
  }, [abortRestore]);

  const cancel = useCallback(() => {
    const initiator = initiatorRef.current;
    initiatorRef.current = null;
    setArmed(null);
    // Cancel always restores the initiating control, which by definition
    // still exists — cancelling destroyed nothing.
    scheduleRestore(initiator);
  }, [scheduleRestore]);

  const close = useCallback((focusAfter?: () => HTMLElement | null) => {
    initiatorRef.current = null;
    setArmed(null);
    // The resolver still runs NOW, because the sites resolve a node that the
    // operation is about to remove from the DOM and hand back that same node.
    scheduleRestore(focusAfter ? focusAfter() : null);
  }, [scheduleRestore]);

  const isArmed = useCallback((id: string) => armed === id, [armed]);

  // Exactly ONE Escape binding per host, live only while something is
  // armed — so the host's own Escape (close the modal, dismiss the popover)
  // behaves exactly as before the moment nothing is awaiting confirmation.
  useKeymap(useMemo(() => [{
    key: 'Escape', scope: 'overlay' as const, layer: CONFIRM_LAYER,
    when: () => armed != null, action: cancel,
  }], [armed, cancel]));

  return { armed, arm, cancel, close, isArmed };
}

export interface ConfirmActionProps {
  /** Identity within the host. Must be unique per confirmable control. */
  id: string;
  host: ConfirmHost;
  /** What the user is about to do, stated in full. */
  prompt: string;
  confirmLabel: string;
  cancelLabel?: string;
  onConfirm: () => void;
  className?: string;
}

/**
 * The confirmation group itself. Renders nothing until its host arms `id`,
 * so a site places it adjacent to its trigger and the trigger stays put —
 * which is what keeps Cancel's restore target alive.
 */
export function ConfirmAction({
  id, host, prompt, confirmLabel, cancelLabel = 'Cancel', onConfirm,
  className,
}: ConfirmActionProps) {
  const promptId = `share-confirm-${id.replace(/[^a-zA-Z0-9_-]+/g, '-')}`;
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  const isOpen = host.isArmed(id);

  useEffect(() => {
    if (isOpen) confirmRef.current?.focus();
  }, [isOpen]);

  if (!isOpen) return null;
  return (
    <span
      className={`share-confirm${className ? ` ${className}` : ''}`}
      role="group"
    >
      {/* `role="status"` is an implicit polite live region, and the Confirm
          button points at it with aria-describedby — so the prompt reaches a
          screen-reader user on focus, which is the moment it matters. */}
      <span className="share-confirm-prompt" id={promptId} role="status">
        {prompt}
      </span>
      <button
        ref={confirmRef}
        type="button"
        className="share-confirm-yes"
        aria-describedby={promptId}
        onClick={onConfirm}
      >
        {confirmLabel}
      </button>
      <button
        type="button"
        className="share-confirm-no"
        onClick={() => host.cancel()}
      >
        {cancelLabel}
      </button>
    </span>
  );
}
