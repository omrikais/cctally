// #703 + #707 §6.3/§6.4 — the credited-week vocabulary the dashboard renders.
//
// A credited week is ONE row on its own boundaries now, so nothing else on
// screen explains a low `Used %` late in a heavy week: the row shows the whole
// week's spend beside a counter Anthropic reset partway through it. The marker
// is that explanation, in the same place `Now` sits.
//
// The `$/1%` of such a week is measured from the credit forward, over the climb
// since it. When the counter has not climbed past the level it was credited to
// there is no divisor the epoch supports, and the server withholds the ratio
// with a typed cause rather than rendering it. An em-dash there would read as
// "no usage recorded", which is a different and wrong statement about a week
// that holds a credit — so the cause is what gets rendered.

export const CREDIT_MARKER_LABEL = 'Credited';

export const CREDIT_MARKER_TITLE =
  'Anthropic credited the counter inside this week. The week keeps its own '
  + 'boundaries, and $/1% is measured from the credit forward.';

/** Typed causes the server emits in `dollar_per_pct_withheld`. */
const WITHHELD_LABELS: Record<string, string> = {
  'no-climb-since-credit': 'No climb since credit',
};

/**
 * The human phrase for a withheld `$/1%`, or `null` when nothing is withheld.
 *
 * An unrecognized cause is returned verbatim rather than dropped: the server
 * may add one, and swallowing it would put the reader back in front of the
 * bare em-dash this exists to replace.
 */
export function withheldDollarPerPctLabel(
  cause: string | null | undefined,
): string | null {
  if (cause == null || cause === '') return null;
  return WITHHELD_LABELS[cause] ?? cause;
}

/**
 * Short forms for the `$/1%` TABLE column, whose cells hold figures.
 *
 * The Weekly modal's table and the `$/1%` Trend panel print this column beside
 * `$49.78` and `12%`, so the full phrase above does not fit: it would widen the
 * column past every figure in it, or wrap. This is the same problem the
 * terminal's `$/1%` column has, and the same answer — `_DPP_WITHHELD_CELL` in
 * `bin/_lib_render.py` maps the identical cause to `no climb` and falls back to
 * `withheld` — so the words here are that vocabulary in the sentence case the
 * dashboard writes its own labels in, not a second one.
 *
 * An unrecognized cause CANNOT be pasted in verbatim the way the detail card
 * pastes it: a server-minted code has no length bound and this column has no
 * room. It renders the short generic word instead, which still says what is
 * true — the ratio is withheld, not absent — and the caller carries the exact
 * cause in the cell's `title` through `withheldDollarPerPctLabel`.
 */
const WITHHELD_CELL_LABELS: Record<string, string> = {
  'no-climb-since-credit': 'No climb',
};

const WITHHELD_CELL_FALLBACK = 'Withheld';

export function withheldDollarPerPctCellLabel(
  cause: string | null | undefined,
): string | null {
  if (cause == null || cause === '') return null;
  return WITHHELD_CELL_LABELS[cause] ?? WITHHELD_CELL_FALLBACK;
}
