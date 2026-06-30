// App-level cold-start placeholder (#248). Mimics the new shape — a hero-strip
// placeholder + a tile-strip row of uniform tiles + a few full-width wide
// placeholders — so the load→ready swap doesn't reflow. (The real HeroStrip and
// two-tier grid only mount once data arrives; this is a pure structural echo.)
// Visual shimmer is aria-hidden; a single sr-only live region conveys "loading"
// to AT. The shimmer animation is gated in CSS by prefers-reduced-motion. Not
// drag-enabled and registers no keymap — a pure placeholder.
const TILE_PLACEHOLDERS = 5;
const WIDE_PLACEHOLDERS = 3;

function SkelCard() {
  return (
    <div className="panel is-skeleton">
      <div className="skel skel-header" />
      <div className="skel skel-line" />
      <div className="skel skel-line short" />
    </div>
  );
}

export function SkeletonGrid() {
  return (
    <>
      <span className="sr-only" role="status" aria-live="polite">Loading dashboard…</span>
      {/* Hero-strip placeholder (echoes the at-a-glance hero). */}
      <div className="skel-hero" aria-hidden="true" />
      <div className="dash-grid" aria-hidden="true">
        <div className="tile-strip">
          {Array.from({ length: TILE_PLACEHOLDERS }).map((_, i) => (
            <SkelCard key={i} />
          ))}
        </div>
        <div className="wide-strip">
          {Array.from({ length: WIDE_PLACEHOLDERS }).map((_, i) => (
            <SkelCard key={i} />
          ))}
        </div>
      </div>
    </>
  );
}
