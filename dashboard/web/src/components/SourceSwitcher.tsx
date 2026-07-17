import { useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import type { DashboardSelection } from '../types/envelope';

// #294 S5 — the global Claude / Codex / All source selector (§5.4).
//
// A three-segment radiogroup rendered for the DASHBOARD workspace only. ARIA is
// the standard radiogroup pattern: role="radiogroup" with a visible group label;
// each segment is role="radio" with aria-checked; ONE roving tab stop (the
// checked segment is tabbable, the rest tabIndex=-1); Left/Right (and Up/Down)
// move focus AND selection together with wrap; Home/End jump to first/last.
// There are NO disabled segments — an `unavailable`/`empty` source stays
// selectable and its accessible name includes its availability when degraded.
// A polite aria-live region announces the switch (DOM mutation only; no audible
// claim is made). Reuses the `.view-switcher`/`.view-seg` CSS vocabulary under
// new class names.

const SEGMENTS: ReadonlyArray<{ value: DashboardSelection; label: string }> = [
  { value: 'claude', label: 'Claude' },
  { value: 'codex', label: 'Codex' },
  { value: 'all', label: 'All' },
];

export function SourceSwitcher() {
  const active = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const view = useSyncExternalStore(subscribeStore, () => getState().view);
  const env = useSnapshot();
  const segRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [announce, setAnnounce] = useState('');

  // Dashboard workspace only — the Conversations workspace is out of scope for
  // the source selector (§5.4 / D1). All hooks run before this early return.
  if (view !== 'dashboard') return null;

  const availabilityOf = (value: DashboardSelection): string | undefined =>
    env?.sources?.[value]?.availability;

  const select = (i: number): void => {
    const seg = SEGMENTS[i];
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: seg.value });
    segRefs.current[i]?.focus();
    setAnnounce(`${seg.label} source selected`);
  };

  const onKeyDown = (e: React.KeyboardEvent, i: number): void => {
    let next: number;
    switch (e.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        next = (i + 1) % SEGMENTS.length;
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        next = (i - 1 + SEGMENTS.length) % SEGMENTS.length;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = SEGMENTS.length - 1;
        break;
      default:
        return;
    }
    e.preventDefault();
    select(next);
  };

  return (
    <div className="source-switcher" role="radiogroup" aria-label="Data source">
      {SEGMENTS.map((seg, i) => {
        const checked = seg.value === active;
        const availability = availabilityOf(seg.value);
        const degraded = availability != null && availability !== 'ok';
        // Accessible name carries availability only when degraded (§5.4).
        const accessibleName = degraded ? `${seg.label} (${availability})` : seg.label;
        return (
          <button
            key={seg.value}
            ref={(el) => {
              segRefs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            aria-label={accessibleName}
            // Roving tab stop: only the checked segment is in the tab order.
            tabIndex={checked ? 0 : -1}
            className={`source-seg${checked ? ' is-active' : ''}`}
            data-source={seg.value}
            onClick={() => select(i)}
            onKeyDown={(e) => onKeyDown(e, i)}
          >
            {seg.label}
          </button>
        );
      })}
      {/* Polite announcement of the active source on switch. */}
      <div className="sr-only" role="status" aria-live="polite" data-testid="source-switcher-live">
        {announce}
      </div>
    </div>
  );
}
