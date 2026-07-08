// #278 Theme A §1.4 — per-panel first-paint loading skeleton.
//
// Rendered inside a heavy panel's body while the envelope is `hydrating` AND
// that panel has no data yet (the cheap bind-before-build seed only fills the
// two headline panels; everything else hydrates over SSE). It replaces the
// panel's definitive empty/"unavailable" copy so first paint doesn't flash a
// broken-looking "No data" / "restart the dashboard" state that then fills in.
//
// Shimmer is aria-hidden and gated by prefers-reduced-motion in CSS (mirrors
// the cold-start SkeletonGrid); a single sr-only status line conveys "loading"
// to assistive tech.
export function PanelSkeleton({
  lines = 3,
  label = 'Loading',
}: {
  lines?: number;
  label?: string;
}) {
  return (
    <div className="panel-skeleton">
      <span className="sr-only" role="status" aria-live="polite">
        {`${label}…`}
      </span>
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className={`skel skel-line${i === lines - 1 ? ' short' : ''}`}
          aria-hidden="true"
        />
      ))}
    </div>
  );
}
