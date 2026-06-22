import { Fragment } from 'react';
import type { AlignmentRow } from './sessionAlign';
import { ExpandedPrompt } from './ExpandedPrompt';

// #217 S7 F10 — the aligned prompt diff. ONE AlignmentRow[] feeds BOTH renderers
// (chosen by `wide`, from useIsWide): a two-column side-by-side grid on desktop,
// a unified single column below 1100px. Clicking any row toggles a lazy
// full-text expand panel beneath it. Divergence is conveyed by the ⚡ DIVERGENCE
// bar + the ◆ markers + the add/del classes — NEVER color alone (a11y).

export interface ComparisonDiffProps {
  rows: AlignmentRow[];
  wide: boolean; // from useIsWide()
  expandedKey: string | null; // `${a?.uuid}|${b?.uuid}`
  onToggleRow: (key: string) => void;
  promptsA: Record<string, string>; // uuid -> full text (partial/empty until loaded)
  promptsB: Record<string, string>;
  onOpenInReader: (side: 'a' | 'b', uuid: string) => void;
}

export const rowKey = (row: AlignmentRow): string =>
  `${row.a?.uuid ?? ''}|${row.b?.uuid ?? ''}`;

// The divergence bar renders ONCE above each maximal contiguous run of
// divergence rows. We track the previous row's divergence flag while mapping.
function DivergenceBar() {
  return (
    <div className="conv-cmp-divbar" role="note">
      <span aria-hidden="true">⚡ </span>DIVERGENCE
    </div>
  );
}

export function ComparisonDiff(props: ComparisonDiffProps) {
  const { rows, wide, expandedKey, onToggleRow, promptsA, promptsB, onOpenInReader } = props;
  let prevDivergence = false;
  return (
    <div
      className={`conv-cmp-diff ${wide ? 'conv-cmp-diff--wide' : 'conv-cmp-diff--unified'}`}
      role="list"
    >
      {rows.map((row, i) => {
        const key = rowKey(row);
        const startRegion = row.divergence && !prevDivergence;
        prevDivergence = row.divergence;
        const expanded = expandedKey === key;
        return (
          <Fragment key={`${key}|${i}`}>
            {startRegion && <DivergenceBar />}
            {wide ? (
              <WideRow row={row} expanded={expanded} onToggle={() => onToggleRow(key)} />
            ) : (
              <UnifiedRow row={row} expanded={expanded} onToggle={() => onToggleRow(key)} />
            )}
            {expanded && (
              <ExpandedPrompt
                aUuid={row.a?.uuid ?? null}
                bUuid={row.b?.uuid ?? null}
                aText={row.a ? promptsA[row.a.uuid] : undefined}
                bText={row.b ? promptsB[row.b.uuid] : undefined}
                onOpenInReader={onOpenInReader}
              />
            )}
          </Fragment>
        );
      })}
    </div>
  );
}

// Two-column row: an A cell + a B cell. `match` → both neutral; `replace` → A
// del / B add; `aOnly` → A cell + hatched-gap B; `bOnly` → hatched-gap A + B
// cell. The whole row is the toggle (a button) so a keyboard user can expand it.
function WideRow({
  row,
  expanded,
  onToggle,
}: {
  row: AlignmentRow;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className={`conv-cmp-row conv-cmp-row--${row.kind}${expanded ? ' is-expanded' : ''}`}
      role="listitem"
      aria-expanded={expanded}
      onClick={onToggle}
    >
      <Cell side="a" row={row} />
      <Cell side="b" row={row} />
    </button>
  );
}

function Cell({ side, row }: { side: 'a' | 'b'; row: AlignmentRow }) {
  const prompt = side === 'a' ? row.a : row.b;
  if (!prompt) {
    return <span className="conv-cmp-cell conv-cmp-cell--gap" aria-hidden="true" />;
  }
  // del = removed side (A) of a replace; add = added side (B) of a replace.
  let tone = 'conv-cmp-cell--match';
  let marker: string | null = null;
  if (row.kind === 'replace') {
    tone = side === 'a' ? 'conv-cmp-cell--del' : 'conv-cmp-cell--add';
    marker = '◆'; // #227 — same diamond on both sides of a replace (tone carries the side)
  } else if (row.kind === 'aOnly') {
    tone = 'conv-cmp-cell--del';
  } else if (row.kind === 'bOnly') {
    tone = 'conv-cmp-cell--add';
  }
  return (
    <span className={`conv-cmp-cell ${tone}`}>
      {marker && <span className="conv-cmp-cell-marker" aria-hidden="true">{marker} </span>}
      <span className="conv-cmp-cell-label">{prompt.label}</span>
    </span>
  );
}

// Unified single column: `match` → one neutral row; `replace` → an A "−" row then
// a B "+" row, each tagged A/B; `aOnly` → an A "−" row; `bOnly` → a B "+" row.
function UnifiedRow({
  row,
  expanded,
  onToggle,
}: {
  row: AlignmentRow;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className={`conv-cmp-urow conv-cmp-urow--${row.kind}${expanded ? ' is-expanded' : ''}`}
      role="listitem"
      aria-expanded={expanded}
      onClick={onToggle}
    >
      {row.kind === 'match' && row.a && (
        <span className="conv-cmp-uline conv-cmp-uline--match">
          <span className="conv-cmp-uline-label">{row.a.label}</span>
        </span>
      )}
      {(row.kind === 'replace' || row.kind === 'aOnly') && row.a && (
        <span className="conv-cmp-uline conv-cmp-uline--del">
          <span className="conv-cmp-uline-sign" aria-hidden="true">− </span>
          <span className="conv-cmp-uline-tag">A</span>
          <span className="conv-cmp-uline-label">{row.a.label}</span>
        </span>
      )}
      {(row.kind === 'replace' || row.kind === 'bOnly') && row.b && (
        <span className="conv-cmp-uline conv-cmp-uline--add">
          <span className="conv-cmp-uline-sign" aria-hidden="true">+ </span>
          <span className="conv-cmp-uline-tag">B</span>
          <span className="conv-cmp-uline-label">{row.b.label}</span>
        </span>
      )}
    </button>
  );
}
