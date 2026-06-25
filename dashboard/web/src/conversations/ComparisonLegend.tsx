// #228 S5 E8 — a one-line key for the diff encoding, pinned between the
// metrics strip and the diff. The "absent"/hatched entry is wide-mode-only
// (unified has no hatched gap). Glyphs reuse the .sem-* colour classes.
export function ComparisonLegend({ wide }: { wide: boolean }) {
  return (
    <div className="conv-cmp-legend" role="note" aria-label="Diff legend">
      <span className="conv-cmp-legend-key">Key</span>
      <span className="conv-cmp-legend-item"><span className="conv-cmp-legend-g sem-match" aria-hidden="true">=</span> matched</span>
      <span className="conv-cmp-legend-item"><span className="conv-cmp-legend-g sem-del" aria-hidden="true">−</span> only in A</span>
      <span className="conv-cmp-legend-item"><span className="conv-cmp-legend-g sem-add" aria-hidden="true">+</span> only in B</span>
      {wide && <span className="conv-cmp-legend-item"><span className="conv-cmp-legend-gap" aria-hidden="true" /> absent</span>}
      <span className="conv-cmp-legend-item"><span className="conv-cmp-legend-g conv-cmp-legend-g--div" aria-hidden="true">⚡</span> divergence</span>
    </div>
  );
}
