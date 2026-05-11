// Header indicator for the share-report basket (spec §7.5-§7.6).
//
// DOM-removed when the basket is empty so the header stays visually
// quiet during normal browsing. Spec is explicit it must NOT be
// `aria-hidden` only — that would still occupy layout. When count > 0
// the chip renders an icon + amber count badge.
//
// Click → composer modal (spec §8.1). The composer dispatch lives in
// `shareSlice.openComposer()`; we forward to it so the keymap (§12.1,
// the `B` hotkey added later) and the chip click share one path.
//
// Pulse animation: when the basket count grows we briefly toggle a
// `basket-chip-pulse` class on the wrapper so the CSS keyframe runs.
// Gated on `prefers-reduced-motion` via CSS (the `.basket-chip-pulse`
// rule is inert under `reduce`). The effect tracks the previous count
// in a ref so we only pulse on growth, not on removal.
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { getState, dispatch, subscribeStore } from '../store/store';
import { openComposer } from '../store/shareSlice';

const PULSE_DURATION_MS = 320;

function selectBasket() {
  return getState().basket;
}

export function BasketChip() {
  const basket = useSyncExternalStore(subscribeStore, selectBasket);
  const count = basket.items.length;

  const [pulse, setPulse] = useState(false);
  const prevCountRef = useRef(count);

  useEffect(() => {
    if (count > prevCountRef.current) {
      setPulse(true);
      const t = setTimeout(() => setPulse(false), PULSE_DURATION_MS);
      prevCountRef.current = count;
      return () => clearTimeout(t);
    }
    prevCountRef.current = count;
  }, [count]);

  if (count === 0) return null;
  const noun = count === 1 ? 'section' : 'sections';
  return (
    <button
      type="button"
      className={`basket-chip${pulse ? ' basket-chip-pulse' : ''}`}
      aria-label={`Open basket (${count} ${noun}). Click to compose.`}
      title={`Basket — ${count} ${noun}. Click to compose.`}
      onClick={() => dispatch(openComposer())}
    >
      {/* Inline clipboard glyph — `icons.svg` doesn't ship a clipboard
          symbol and the ShareIcon precedent is to inline the SVG so the
          component stays self-contained. Spec §7.5 ASCII calls out a
          clipboard glyph. */}
      <svg
        className="basket-chip-icon"
        width="14"
        height="14"
        viewBox="0 0 14 14"
        aria-hidden="true"
        focusable="false"
      >
        <rect
          x="2.75"
          y="2"
          width="8.5"
          height="10.5"
          rx="1"
          fill="none"
          stroke="currentColor"
          strokeWidth="1"
        />
        <rect
          x="4.5"
          y="0.75"
          width="5"
          height="2.5"
          rx="0.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1"
        />
      </svg>
      <span className="basket-chip-count">{count}</span>
    </button>
  );
}
