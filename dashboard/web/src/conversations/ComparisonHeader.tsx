import type { ConversationSource } from '../types/conversation';

// #217 S7 F10 — the comparison header bar: a "⟷ Comparing" title, the two
// per-side column labels (each run's display title + date + model), and the
// ⇄ swap / ✕ close controls. The per-side `{ title, date, model }` are derived
// by the parent from the rail ConversationSummary (preferred) with outline
// fallbacks (turns[0].ts for the date, stats.models keys for the model) — the
// outline itself carries no title/date/model, so the parent resolves them.
export interface SideHeader {
  title: string;
  date: string | null;
  model: string | null;
  source?: ConversationSource;
}

export function ComparisonHeader({
  a,
  b,
  onSwap,
  onExport,
  exportBusy = false,
  onClose,
}: {
  a: SideHeader;
  b: SideHeader;
  onSwap: () => void;
  onExport?: () => void;
  exportBusy?: boolean;
  onClose: () => void;
}) {
  return (
    <div className="conv-cmp-head">
      <div className="conv-cmp-head-title">
        <span className="conv-cmp-head-glyph" aria-hidden="true">⟷ </span>
        Comparing
      </div>
      <div className="conv-cmp-head-sides">
        <Side side={a} label="Run A" tone="a" />
        <Side side={b} label="Run B" tone="b" />
      </div>
      <div className="conv-cmp-head-controls">
        {onExport && (
          <button
            type="button"
            className="conv-cmp-export"
            aria-label="Copy source-labelled comparison export"
            disabled={exportBusy}
            onClick={onExport}
          >
            {exportBusy ? 'Copying…' : 'Copy export'}
          </button>
        )}
        <button
          type="button"
          className="conv-cmp-swap"
          aria-label="Swap the two sessions"
          title="Swap sides"
          onClick={onSwap}
        >
          ⇄ Swap
        </button>
        <button
          type="button"
          className="conv-cmp-close"
          aria-label="Close comparison"
          title="Close comparison"
          onClick={onClose}
        >
          ✕ Close
        </button>
      </div>
    </div>
  );
}

// #304 S2 (F5) — the Run A/B tags are real text for AT; hiding them left run
// identity unassociated with the titles. Reading order per side is now
// "Run A, <title>, <date · model>" — identity associated by document order.
function Side({ side, label, tone }: { side: SideHeader; label: string; tone: 'a' | 'b' }) {
  const meta = [side.date, side.model].filter(Boolean).join(' · ');
  return (
    <div className="conv-cmp-head-side">
      <span className={`conv-cmp-head-side-tag conv-cmp-head-side-tag--${tone}`}>{label}</span>
      {side.source && <span className={`conv-source-badge conv-source-badge--${side.source}`}>{side.source === 'codex' ? 'Codex' : 'Claude'}</span>}
      <span className="conv-cmp-head-side-name">{side.title}</span>
      {meta && <span className="conv-cmp-head-side-meta">{meta}</span>}
    </div>
  );
}
