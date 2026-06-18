interface ConfidenceDotsProps { n: number | null | undefined; }

// Mirrors dashboard/static/render.js#renderConfidenceDots. Emits
// <div class="dots" id="fc-dots"> with 7 <span class="d"> children;
// each i < n gets the `.on` class. Main's CSS in index.css keys off
// `.dots > .d.on` — do NOT rename either selector.
export function ConfidenceDots({ n }: ConfidenceDotsProps) {
  const count = Math.max(0, Math.min(7, (n ?? 0) | 0));
  // A5 — a discrete 0–7 confidence indicator, not a 0–100 gauge, so it
  // gets a role="img" text summary rather than a (wrong) progressbar role.
  return (
    <div className="dots" id="fc-dots" role="img" aria-label={`Confidence: ${count} of 7`}>
      {Array.from({ length: 7 }, (_, i) => (
        <span key={i} className={'d' + (i < count ? ' on' : '')} />
      ))}
    </div>
  );
}
