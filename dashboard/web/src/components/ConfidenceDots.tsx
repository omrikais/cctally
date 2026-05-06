interface ConfidenceDotsProps { n: number | null | undefined; }

// Mirrors dashboard/static/render.js#renderConfidenceDots. Emits
// <div class="dots" id="fc-dots"> with 7 <span class="d"> children;
// each i < n gets the `.on` class. Main's CSS in index.css keys off
// `.dots > .d.on` — do NOT rename either selector.
export function ConfidenceDots({ n }: ConfidenceDotsProps) {
  const count = Math.max(0, Math.min(7, (n ?? 0) | 0));
  return (
    <div className="dots" id="fc-dots">
      {Array.from({ length: 7 }, (_, i) => (
        <span key={i} className={'d' + (i < count ? ' on' : '')} />
      ))}
    </div>
  );
}
