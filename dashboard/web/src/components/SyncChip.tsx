import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { useConnectionStatus } from '../hooks/useConnectionStatus';
import { getState, subscribeStore } from '../store/store';

// Mirrors dashboard/static/render.js#updateSyncChip + its 1-second tick.
// Guards: never overwrite "disconnected"; respect the 3-second sync
// error floor set by triggerSync() on a failed POST /api/sync. The
// floor lives in store state (syncErrorFloorUntil, epoch-ms) so the
// chip's own tick can consult it without a ref; a single timeout wakes
// the chip up at the expiry boundary so the post-floor state paints
// immediately instead of lagging up to 1 s behind the next tick.

export function SyncChip() {
  const env = useSnapshot();
  const { disconnected } = useConnectionStatus();
  const floorUntil = useSyncExternalStore(
    subscribeStore,
    () => getState().syncErrorFloorUntil,
  );
  const busy = useSyncExternalStore(
    subscribeStore,
    () => getState().syncBusy,
  );
  const successFlashUntil = useSyncExternalStore(
    subscribeStore,
    () => getState().syncSuccessFlashUntil,
  );
  const [text, setText] = useState('sync paused');
  const [color, setColor] = useState('');
  const tickOffset = useRef(0);
  // Nudges the chip to re-evaluate when the error floor expires so the
  // post-floor text/class paint the moment it times out.
  const [, setFloorExpiredNonce] = useState(0);
  // Same idea for the success-flash expiry — without it the "✓ updated"
  // state would linger up to a second past its 1.2 s deadline before
  // the next env tick repainted the chip.
  const [, setSuccessFlashExpiredNonce] = useState(0);

  const now = Date.now();
  const floorActive = now < floorUntil;
  const successFlashActive = now < successFlashUntil;

  // Paint from envelope each time it changes. Skipped while the error
  // floor or success flash is active — the render branches below
  // override text/color/class.
  useEffect(() => {
    if (floorActive || successFlashActive) return;
    if (disconnected) { setText('disconnected'); setColor('var(--accent-red)'); return; }
    if (!env) return;
    if (env.last_sync_error) {
      setText('⚠ sync error');
      setColor('var(--accent-red)');
      return;
    }
    if (env.sync_age_s == null) {
      setText('sync paused'); setColor('');
      tickOffset.current = 0;
      return;
    }
    setColor('');
    tickOffset.current = env.sync_age_s | 0;
    setText(`synced ${tickOffset.current}s ago`);
  }, [env, disconnected, floorActive, successFlashActive]);

  // 1-second tick. Also suppressed while the error floor is active.
  useEffect(() => {
    const id = window.setInterval(() => {
      if (disconnected) return;
      if (Date.now() < getState().syncErrorFloorUntil) return;
      if (!env || env.sync_age_s == null || env.last_sync_error) return;
      tickOffset.current += 1;
      setText(`synced ${tickOffset.current}s ago`);
    }, 1000);
    return () => window.clearInterval(id);
  }, [env, disconnected]);

  // Schedule a single re-render at the exact moment the floor expires,
  // so the chip stops showing "⚠ sync failed" without waiting up to 1 s
  // for the tick. Guards against negative delay if the floor was set in
  // the past (useEffect cleanup covers unmount / floor change).
  useEffect(() => {
    if (!floorActive) return;
    const delay = floorUntil - Date.now();
    if (delay <= 0) return;
    const id = window.setTimeout(
      () => setFloorExpiredNonce((n) => n + 1),
      delay,
    );
    return () => window.clearTimeout(id);
  }, [floorUntil, floorActive]);

  // Same wake-at-expiry pattern for the success flash.
  useEffect(() => {
    if (!successFlashActive) return;
    const delay = successFlashUntil - Date.now();
    if (delay <= 0) return;
    const id = window.setTimeout(
      () => setSuccessFlashExpiredNonce((n) => n + 1),
      delay,
    );
    return () => window.clearTimeout(id);
  }, [successFlashUntil, successFlashActive]);

  // Render priority: busy > error-floor > success-flash > default.
  // A click during error-floor (3 s) starts a new request; the user
  // is retrying and wants to see "syncing…" progress, not the prior
  // failure's red text. Error wins over success when both timer-active
  // (rare overlap from a click during a prior failure) — error is
  // louder and more important to surface. Success-flash is the lowest-
  // priority overlay; the chip falls through to env-driven default
  // when none of the three states is active.
  //
  // Renders a span (not a button) — the parent .topbar-sync wrapper is
  // the click target so the whole sync icon + status pill is one
  // tappable area, and on mobile the chip text is visually-hidden
  // (sr-only) while the icon carries the visible signal. triggerSync()
  // lives on the wrapper.
  //
  // aria-live="polite" surfaces text changes to screen readers without
  // interrupting the user mid-sentence; mobile-sighted users get the
  // icon's :has()-driven color flash, while screen readers get the
  // chip's text-level state announcements through the sr-only span.
  if (busy) {
    return (
      <span
        className="sync-chip mute syncing"
        id="sync-chip"
        aria-busy="true"
        aria-live="polite"
      >
        syncing…
      </span>
    );
  }
  if (floorActive) {
    return (
      <span
        className="sync-chip mute sync-error"
        id="sync-chip"
        aria-live="polite"
        style={{ color: 'var(--accent-red)' }}
      >
        ⚠ sync failed
      </span>
    );
  }
  if (successFlashActive) {
    return (
      <span
        className="sync-chip mute sync-success"
        id="sync-chip"
        aria-live="polite"
      >
        ✓ updated
      </span>
    );
  }
  return (
    <span
      className="sync-chip mute"
      id="sync-chip"
      aria-live="polite"
      style={{ color }}
    >
      {text}
    </span>
  );
}
