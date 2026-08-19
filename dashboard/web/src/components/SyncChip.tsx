import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { useConnectionStatus } from '../hooks/useConnectionStatus';
import {
  effectiveSnapshotAge, getState, subscribeStore,
} from '../store/store';
import {
  moreSevereBucket,
  syncFreshness,
  type SyncBucket,
} from '../lib/syncFreshness';
import { syncActivityOrIdle } from '../lib/syncActivity';

function bucketClass(bucket: SyncBucket | ''): string {
  return bucket ? ` sync-chip--${bucket}` : '';
}

// Mirrors dashboard/static/render.js#updateSyncChip + its 1-second tick.
// Guards: never overwrite "disconnected"; respect the 3-second sync
// error floor set by triggerSync() on a failed POST /api/sync. The
// floor lives in store state (syncErrorFloorUntil, epoch-ms) so the
// chip's own tick can consult it without a ref; a single timeout wakes
// the chip up at the expiry boundary so the post-floor state paints
// immediately instead of lagging up to 1 s behind the next tick.

export function SyncChip({ id = 'sync-chip' }: { id?: string } = {}) {
  const env = useSnapshot();
  const { disconnected, state: connection } = useConnectionStatus();
  const snapshotObservedAtMs = useSyncExternalStore(
    subscribeStore,
    () => getState().snapshotObservedAtMs,
  );
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
  const busyNoticeUntil = useSyncExternalStore(
    subscribeStore,
    () => getState().syncBusyNoticeUntil,
  );
  // #583 S2 §6.3 — this tab's own queued request, if any. `null` when nothing
  // is outstanding; the store clears it the moment a frame proves it settled.
  const outstandingSyncId = useSyncExternalStore(
    subscribeStore,
    () => getState().outstandingSyncId,
  );
  const [text, setText] = useState('sync paused');
  const [color, setColor] = useState('');
  // S7 SYNC-1: freshness bucket for the default (env-driven) branch only;
  // '' when a non-default state (busy/error/paused/disconnected) owns the
  // chip so those keep their own colors.
  const [bucket, setBucket] = useState<SyncBucket | ''>('');
  const tickOffset = useRef(0);
  // Nudges the chip to re-evaluate when the error floor expires so the
  // post-floor text/class paint the moment it times out.
  const [, setFloorExpiredNonce] = useState(0);
  // Same idea for the success-flash expiry — without it the "✓ updated"
  // state would linger up to a second past its 1.2 s deadline before
  // the next env tick repainted the chip.
  const [, setSuccessFlashExpiredNonce] = useState(0);
  const [, setBusyNoticeExpiredNonce] = useState(0);

  const now = Date.now();
  const floorActive = now < floorUntil;
  const successFlashActive = now < successFlashUntil;
  const busyNoticeActive = now < busyNoticeUntil;
  const queued = outstandingSyncId != null;
  // A rebuild the SERVER is running, whether or not anyone asked for it. On a
  // healthy dashboard measured work is 1.9–6.3 s against a 6.9–12.7 s period,
  // so this is true roughly a quarter to a half of the time. That visible pulse
  // IS the point (#583 F19): today nothing pulses at all, so a busy dashboard
  // and a wedged one look identical.
  const rebuilding = syncActivityOrIdle(env).rebuilding;
  const effectiveAge = effectiveSnapshotAge(
    env?.sync_age_s ?? null,
    snapshotObservedAtMs,
    now,
  );
  // The freshness bucket describes the DATA; the label describes the ACTIVITY.
  // The two are independent axes, so an activity label must not suppress the
  // bucket: on mobile the chip text is sr-only and the bucket-driven dot is the
  // only staleness channel a sighted user has, and a rebuild is in flight 42–52%
  // of wall-clock time. Read from the envelope on every render so a branch other
  // than the default still has a value on its first paint, before the effect
  // below has run.
  const envBucket: SyncBucket | '' =
    !disconnected && env && !env.last_sync_error && effectiveAge != null
      ? syncFreshness(effectiveAge).bucket
      : '';
  // Two readings of the same freshness, and neither may be preferred
  // unconditionally. The `bucket` state is computed from the age the chip is
  // actually showing, which the 1-second tick advances past the envelope's own
  // age; but the paint effect returns early while the error floor or the success
  // flash is active, so an envelope that arrives during those three seconds
  // leaves `bucket` one commit behind. Age only increases within one sync, so
  // the more severe of the two is the later one either way.
  const activityBucket = moreSevereBucket(bucket, envBucket);

  // Paint from envelope each time it changes. Skipped while the error
  // floor or success flash is active — the render branches below
  // override text/color/class.
  useEffect(() => {
    if (floorActive || successFlashActive) return;
    if (disconnected) { setText('disconnected'); setColor('var(--accent-red)'); setBucket(''); return; }
    if (!env) return;
    if (env.last_sync_error) {
      // The server-failure branch below owns this state's label, color and
      // class, so the effect only has to drop the bucket the default branch
      // would otherwise carry forward from the last clean envelope.
      setBucket('');
      return;
    }
    if (env.sync_age_s == null) {
      setText('sync paused'); setColor('');
      setBucket('');
      tickOffset.current = 0;
      return;
    }
    setColor('');
    tickOffset.current = effectiveSnapshotAge(
      env.sync_age_s,
      snapshotObservedAtMs,
      Date.now(),
    ) ?? 0;
    {
      const f = syncFreshness(tickOffset.current);
      setText(`synced ${f.text}`);
      setBucket(f.bucket);
    }
  }, [env, disconnected, floorActive, snapshotObservedAtMs, successFlashActive]);

  // 1-second tick. Also suppressed while the error floor is active.
  useEffect(() => {
    const id = window.setInterval(() => {
      if (disconnected) return;
      if (Date.now() < getState().syncErrorFloorUntil) return;
      if (!env || env.sync_age_s == null || env.last_sync_error) return;
      tickOffset.current = effectiveSnapshotAge(
        env.sync_age_s,
        snapshotObservedAtMs,
        Date.now(),
      ) ?? tickOffset.current;
      const f = syncFreshness(tickOffset.current);
      setText(`synced ${f.text}`);
      setBucket(f.bucket);
    }, 1000);
    return () => window.clearInterval(id);
  }, [env, disconnected, snapshotObservedAtMs]);

  // Schedule a single re-render at the exact moment the floor expires,
  // so the chip stops showing "⚠ sync request failed" without waiting up to 1 s
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

  // A refused refresh is a bounded informational state, so wake at its exact
  // expiry just as the success and failure overlays do.
  useEffect(() => {
    if (!busyNoticeActive) return;
    const delay = busyNoticeUntil - Date.now();
    if (delay <= 0) return;
    const id = window.setTimeout(
      () => setBusyNoticeExpiredNonce((n) => n + 1),
      delay,
    );
    return () => window.clearTimeout(id);
  }, [busyNoticeUntil, busyNoticeActive]);

  // Render priority (#583 S2 §6.3):
  //   busy > queued > error-floor > success-flash > busy-refusal >
  //   server-failure > rebuilding > default.
  //
  // The browser gate moved the standing server failure above `rebuilding`. It
  // used to be handled only inside the default branch, so every rebuild painted
  // over it: a dashboard with a persistently failing leg changed state 26 times
  // in 112 seconds, alternating the failure label and `syncing…`. The failure is
  // the more actionable fact, and it is still true once the rebuild has finished.
  //
  // `queued` and `rebuilding` are additionally gated on `!disconnected`; the
  // reasons are stated at each branch.
  //
  // The two new states sit where they do for one reason each. A local
  // outstanding queued request outranks server `rebuilding` because it is the
  // more specific fact: the user clicked, the server accepted, and this tab is
  // waiting on that identifier — "some rebuild is running" is true but says
  // nothing about their click. Server `rebuilding` sits BELOW the error floor
  // and the success flash because those two report an outcome the user asked
  // for, and a background rebuild must not paint over the answer to a question
  // they just posed.
  //
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
  //
  // A branch carries aria-live when its text settles and then holds: busy,
  // queued, the error floor, the success flash, the server failure, and the
  // default branch when it renders `sync paused` or `disconnected`. Two texts
  // change on their own for as long as the tab stays open and are therefore
  // silent — the `rebuilding` branch, which flips on every rebuild period, and
  // the default branch while it renders the ticking age.
  if (busy) {
    return (
      <span
        className={'sync-chip mute syncing' + bucketClass(activityBucket)}
        id={id}
        aria-busy="true"
        aria-live="polite"
      >
        syncing…
      </span>
    );
  }
  if (queued && !disconnected) {
    // The POST has already answered 202, so nothing is in flight locally — but
    // the request is accepted and unserviced, and saying so is the whole
    // difference between "the server took my click" and the old silent no-op.
    // Reuses `.syncing` deliberately: same pulse, same `:has()` icon mirror, no
    // new CSS class to keep in step with the design system.
    //
    // Suppressed while disconnected for the same reason `rebuilding` is, and
    // more urgently: an outstanding identifier is cleared only by
    // `reconcileOutstandingSync`, which needs a frame. The other local states
    // that outrank the disconnected paint are bounded — `busy` by its in-flight
    // request, the error floor and the success flash by their timers — but a
    // queued identifier is bounded by nothing, so without this guard a tab whose
    // server died mid-request would show `queued…` for the life of the tab. The
    // self-healing paths cover the reconnect: a reconnect to the same process
    // settles from the retained high-water mark, and a restart discards on the
    // epoch change.
    return (
      <span
        className={'sync-chip mute syncing' + bucketClass(activityBucket)}
        id={id}
        aria-busy="true"
        aria-live="polite"
      >
        queued…
      </span>
    );
  }
  if (floorActive) {
    return (
      <span
        className="sync-chip mute sync-error"
        id={id}
        aria-live="polite"
        style={{ color: 'var(--accent-red)' }}
      >
        ⚠ sync request failed
      </span>
    );
  }
  if (successFlashActive) {
    return (
      <span
        className="sync-chip mute sync-success"
        id={id}
        aria-live="polite"
      >
        ✓ updated
      </span>
    );
  }
  if (busyNoticeActive) {
    return (
      <span
        className="sync-chip mute sync-busy-notice"
        id={id}
        aria-live="polite"
        aria-label="Refresh busy. Another rebuild is still running. Try again."
        title="Another rebuild is still running. Try again."
      >
        refresh busy
      </span>
    );
  }
  if (connection === 'suspended') {
    return (
      <span
        className="sync-chip mute"
        id={id}
        aria-live="polite"
      >
        Updates paused while hidden
      </span>
    );
  }
  if (connection === 'resuming') {
    return (
      <span
        className="sync-chip mute"
        id={id}
        aria-live="polite"
      >
        Resuming updates…
      </span>
    );
  }
  // The server's own standing failure, as distinct from the bounded local floor
  // above. It is not a timed overlay: it persists until a tick succeeds, and the
  // server keeps rebuilding while it is still set. Placed ahead of `rebuilding`
  // so a background rebuild does not replace it.
  if (!disconnected && env?.last_sync_error) {
    const failure = env.sync_failure ?? null;
    // The raw `last_sync_error` string can carry a filesystem path, so only the
    // typed block is ever rendered.
    const title = failure
      ? [failure.detail, failure.action].filter(Boolean).join(' ')
      : undefined;
    return (
      <span
        className="sync-chip mute sync-error"
        id={id}
        aria-live="polite"
        aria-label={title}
        title={title}
        style={{ color: 'var(--accent-red)' }}
      >
        {failure?.label ?? '⚠ server sync error'}
      </span>
    );
  }
  if (rebuilding && !disconnected) {
    // The server is rebuilding, and nobody in this tab is waiting on a specific
    // request. Same `syncing…` treatment as a local POST, because from the
    // user's side it is the same fact: the numbers are being recomputed right
    // now. Suppressed while disconnected, where a stale `rebuilding` flag from
    // the last frame received would otherwise claim live progress from a server
    // this tab can no longer reach.
    //
    // This branch sets no aria-live. It flips on every rebuild period, so an
    // announcement here reads the chip out roughly every ten seconds for as long
    // as the tab stays open. `aria-busy` remains, because a user can query it
    // instead of being interrupted by it.
    return (
      <span
        className={'sync-chip mute syncing' + bucketClass(activityBucket)}
        id={id}
        aria-busy="true"
      >
        syncing…
      </span>
    );
  }
  // Reachable texts here are `disconnected`, `sync paused` and the ticking age;
  // the server-failure label is no longer one of them. The first two settle and
  // then hold, so they are announced. The age changes once a second for as long
  // as the tab stays open, which is a noisier live region than the `rebuilding`
  // flip this round silenced, so the age is not.
  const settled = disconnected || env == null || env.sync_age_s == null;
  return (
    <span
      className={
        'sync-chip mute'
        + bucketClass(activityBucket)
        + (disconnected ? ' sync-error' : '')
      }
      id={id}
      aria-live={settled ? 'polite' : undefined}
      style={{ color }}
    >
      {text}
    </span>
  );
}
