import type { ReactNode } from 'react';
import {
  withheldDollarPerPctCellLabel, withheldDollarPerPctLabel,
} from './creditMarker';

// #703 + #707 §6.3 — the ONE decision every `$/1%` table column makes.
//
// Three tables render that column from the same pair of fields: the Weekly
// modal's own table, the `$/1%` Trend panel, and the Trend modal. The rule had
// been written once, on the weekly detail card, and a real-browser pass found
// the table four lines to its right printing a bare em-dash for the same week —
// the reading `creditMarker.ts` says is wrong, because it states "no usage
// recorded" about a week that holds a credit. Measured: the credited week and
// an uncredited week with no usage rendered the identical glyph.
//
// So the decision lives here and the three tables call it, rather than each
// repeating a null check that one of them will miss again.

/**
 * The withheld-cause cell for a `$/1%` column, or `null` when the column should
 * render its ordinary contents.
 *
 * Returns `null` when a ratio was published — a rendered figure always wins, so
 * a stale cause can never replace one — and when nothing is withheld, which
 * leaves the caller's own absent-value rendering (an em-dash, or the Trend
 * modal's `Unavailable`) exactly as it was.
 *
 * The cell text is the SHORT form, because the column holds figures. The full
 * phrase, or the verbatim code of a cause this build does not recognize, rides
 * in the `title` so nothing the server said is lost.
 */
export function dollarPerPctWithheldCell(
  value: number | null | undefined,
  cause: string | null | undefined,
): ReactNode | null {
  if (value != null) return null;
  const short = withheldDollarPerPctCellLabel(cause);
  if (short == null) return null;
  return (
    <span
      className="dpp-withheld"
      title={withheldDollarPerPctLabel(cause) ?? undefined}
    >
      {short}
    </span>
  );
}
