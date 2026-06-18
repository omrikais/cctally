interface Props { kind: 'stale' | 'error'; }

// Shared banner for B2 (SSE disconnect over last-good data) and B3 (failed
// bootstrap with no data yet). role=status + aria-live=polite announce once
// without grabbing focus. A distinct `.stale-banner` class (NOT the verdict
// `.warn-banner`) so the two never collide.
export function ConnectionBanner({ kind }: Props) {
  const text =
    kind === 'stale'
      ? 'Disconnected — data may be stale. Reconnecting…'
      : 'Couldn’t load dashboard data. Reconnecting…';
  return (
    <div className={`stale-banner stale-banner-${kind}`} role="status" aria-live="polite">
      <svg className="icon" aria-hidden="true">
        <use href="/static/icons.svg#warn-triangle" />
      </svg>
      <span>{text}</span>
      {kind === 'error' && (
        <span className="stale-banner-hint">
          Check that <code>cctally dashboard</code> is running.
        </span>
      )}
    </div>
  );
}
