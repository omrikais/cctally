interface Props { count: number; }

// App-level cold-start placeholder. Renders `count` placeholder cards inside
// the SAME `.grid` so the load→ready swap doesn't reflow. Visual shimmer is
// aria-hidden; a single sr-only live region conveys "loading" to AT. The
// shimmer animation itself is gated in CSS by prefers-reduced-motion. The
// skeleton is not drag-enabled and registers no keymap — a pure placeholder.
export function SkeletonGrid({ count }: Props) {
  const n = Math.max(1, count);
  return (
    <>
      <span className="sr-only" role="status" aria-live="polite">Loading dashboard…</span>
      <div className="grid" aria-hidden="true">
        {Array.from({ length: n }).map((_, i) => (
          <div key={i} className="panel is-skeleton">
            <div className="skel skel-header" />
            <div className="skel skel-line" />
            <div className="skel skel-line short" />
          </div>
        ))}
      </div>
    </>
  );
}
