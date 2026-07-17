// App-level cold-start placeholder (#264 S1). Mimics the new bento shape — a
// hero-strip placeholder + three height-class rows (tall / medium / short) of
// span-sized placeholders — so the load→ready swap doesn't reflow. (The real
// HeroStrip and bento grid only mount once data arrives; this is a pure
// structural echo.) Row membership + spans are derived from CARD_LAYOUT so the
// skeleton can never drift from the live layout. Visual shimmer is aria-hidden;
// a single sr-only live region conveys "loading" to AT. The shimmer animation
// is gated in CSS by prefers-reduced-motion. Not drag-enabled and registers no
// keymap — a pure placeholder.
import { DEFAULT_PANEL_ORDER, CARD_LAYOUT } from '../lib/panelIds';
import { boardSpan, type BoardMode } from '../lib/boardLayout';

const ROWS: Array<'tall' | 'medium' | 'short'> = ['tall', 'medium', 'short'];

function SkelCard({ span }: { span: number }) {
  // Mirror the real DOM (`.panel-host[data-span] > .panel`) so the same bento
  // CSS (grid-column span + fixed row height) applies to the placeholder.
  return (
    <div className="panel-host" data-span={span}>
      <div className="panel is-skeleton">
        <div className="skel skel-header" />
        <div className="skel skel-line" />
        <div className="skel skel-line short" />
      </div>
    </div>
  );
}

export function SkeletonGrid({ mode }: { mode: BoardMode }) {
  return (
    <>
      <span className="sr-only" role="status" aria-live="polite">Loading dashboard…</span>
      {/* Hero-strip placeholder (echoes the at-a-glance hero). */}
      <div className="skel-hero" aria-hidden="true" />
      {/* #293 S1 — the skeleton reads the SAME board mode as the live grid so
          the load→ready swap can't reflow (at 900–1199 both tile Sessions-full
          / Trend·Projects-paired instead of the crushed 6/3/3). */}
      <div className="dash-grid" aria-hidden="true" data-board-mode={mode}>
        {ROWS.map((row) => {
          const ids = DEFAULT_PANEL_ORDER.filter((id) => CARD_LAYOUT[id].row === row);
          return (
            <div key={row} className={`bento-row row-${row}`}>
              {ids.map((id) => (
                <SkelCard key={id} span={boardSpan(id, mode)} />
              ))}
            </div>
          );
        })}
      </div>
    </>
  );
}
