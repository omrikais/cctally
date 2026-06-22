import { fmt } from '../lib/fmt';

// #217 S6 F3 — cumulative-cost-through-current-turn chip. Pure: the reader
// computes the prefix-sum (cumulativeCostThrough) keyed off convCurrentTurnUuid
// and passes it here. `approx` (≡ hasPrev) renders a leading ~ to flag a lower
// bound when earlier pages aren't loaded. Hidden for a costless session, and
// (#226 / #217 S6 I-1 P3) while no current turn is established yet (`pending`):
// before the scroll-sync IntersectionObserver first fires nothing has scrolled
// past, so the prefix-sum is 0 — suppress the transient "$0.00 / $total" flash.
// A genuine $0 turn scrolled-past (pending=false) still renders honestly.
export function CumulativeCostChip({
  cumulative, total, approx, pending = false,
}: { cumulative: number; total: number; approx: boolean; pending?: boolean }) {
  if (!(total > 0)) return null;
  if (pending && !(cumulative > 0)) return null;
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
