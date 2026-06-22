import { fmt } from '../lib/fmt';

// #217 S6 F3 — cumulative-cost-through-current-turn chip. Pure: the reader
// computes the prefix-sum (cumulativeCostThrough) keyed off convCurrentTurnUuid
// and passes it here. `approx` (≡ hasPrev) renders a leading ~ to flag a lower
// bound when earlier pages aren't loaded. Hidden for a costless session.
export function CumulativeCostChip({
  cumulative, total, approx,
}: { cumulative: number; total: number; approx: boolean }) {
  if (!(total > 0)) return null;
  const frac = Math.max(0, Math.min(1, cumulative / total));
  return (
    <div
      className="conv-cumcost-chip"
      title="Cumulative cost through the current turn / session total"
      aria-label={`Cumulative cost ${approx ? 'at least ' : ''}${fmt.usd2(cumulative)} of ${fmt.usd2(total)} total`}
    >
      <span className="conv-cumcost-text">
        {approx ? '~' : ''}{fmt.usd2(cumulative)} / {fmt.usd2(total)}
      </span>
      <span className="conv-cumcost-track" aria-hidden="true">
        <span className="conv-cumcost-fill" style={{ ['--conv-cumcost-frac' as string]: String(frac) }} />
      </span>
    </div>
  );
}
