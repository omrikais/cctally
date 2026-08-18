interface Props { kind: 'stale' | 'error'; message?: string | null; }

// Shared banner for B2 (SSE disconnect over last-good data) and B3 (failed
// bootstrap with no data yet). role=status + aria-live=polite announce once
// without grabbing focus. A distinct `.stale-banner` class (NOT the verdict
// `.warn-banner`) so the two never collide.
//
// #583 S3 §7 — `message` overrides the default sentence for an error whose
// raiser knows more than "couldn't load". The default still applies to the
// stream-error case it was written for.
export function ConnectionBanner({ kind, message }: Props) {
  const text =
    kind === 'stale'
      ? 'Disconnected — data may be stale. Reconnecting…'
      : message ?? 'Couldn’t load dashboard data. Reconnecting…';
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
