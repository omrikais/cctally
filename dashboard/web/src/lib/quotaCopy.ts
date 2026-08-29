// #661 S2 section 8 — the client half of the withheld-figure vocabulary.
//
// The wire keeps the machine `code` and ADDS optional presentation fields.
// This module reads those fields off the wire rather than enumerating causes
// client-side, which is how the dashboard accumulated six independently
// maintained cause vocabularies in the first place.
//
// One deterministic named function is the fallback. A new client meeting an
// OLD server receives no presentation object at all, and it must still render
// something: `deriveShortFromCode` reproduces the server's own short-register
// derivation — the hyphens become spaces — so the token cannot disagree with
// the server's, and only the SENTENCE is lost. That matters because a
// dashboard tab can outlive a server restart through `execvp`.

import type { QuotaCausePresentation } from '../types/envelope';

/**
 * The short token, derived from the machine code alone.
 *
 * The whole derivation, mirroring `_lib_quota_copy.derive_short_from_code`.
 * Never returns an empty string for a non-empty code, which is the property
 * the degraded path depends on.
 */
export function deriveShortFromCode(code: string | null | undefined): string {
  const text = (code ?? '').trim();
  if (!text) return '';
  return text.replace(/-/g, ' ');
}

/**
 * The short register for one cause: the server's rendering when it sent one,
 * the derivation otherwise, and `''` when there is no cause at all.
 *
 * An absent cause renders NOTHING rather than a token that looks like one —
 * the caller decides what an absence looks like in its own layout.
 */
export function causeShort(
  presentation: QuotaCausePresentation | null | undefined,
  code: string | null | undefined,
): string {
  if (presentation?.short) return presentation.short;
  return deriveShortFromCode(code);
}

/**
 * The sentence register, or the short token when the server sent no sentence.
 *
 * A client cannot derive a sentence, so this is the field that genuinely
 * degrades against an older server. It degrades to the token, never to a
 * blank.
 */
export function causeLong(
  presentation: QuotaCausePresentation | null | undefined,
  code: string | null | undefined,
): string {
  if (presentation?.long) return presentation.long;
  return deriveShortFromCode(code);
}

/**
 * How a rate transition changed the meter, as a sentence fragment.
 *
 * A HIGHER units-per-point is a more generous rate: more work before the
 * meter moves one point. Returns null when either rate is missing or
 * unusable, because a percentage from an absent operand is not a percentage.
 */
export function rateChangeSummary(
  previous: number | null | undefined,
  next: number | null | undefined,
): string | null {
  if (previous == null || next == null) return null;
  if (!Number.isFinite(previous) || !Number.isFinite(next)) return null;
  if (previous <= 0 || next <= 0) return null;
  const change = ((next - previous) / previous) * 100;
  const direction = change < 0 ? 'less' : 'more';
  return `each meter point now covers ${Math.abs(change).toFixed(0)}% ${direction} usage`;
}
