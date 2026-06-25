// #217 S7 F10 — the per-row expand panel. Shows A's full prompt text (left) and
// B's (right) side-by-side in two-column mode, stacked in unified mode, each
// with an "open in reader →" button that jumps to that session's normal
// single-session reader at the turn. There is deliberately NO A↔B word-diff:
// two divergent prompts are two different texts, so a word-diff would be noise.
//
// `aText`/`bText` come from the lazy /prompts fetch (useConversationPrompts);
// while a side's text is undefined (not yet loaded) we show a "loading…"
// placeholder. A null uuid (a one-sided row's empty side) renders nothing for
// that side.
export function ExpandedPrompt({
  aUuid,
  bUuid,
  aText,
  bText,
  onOpenInReader,
}: {
  aUuid: string | null;
  bUuid: string | null;
  aText: string | undefined;
  bText: string | undefined;
  onOpenInReader: (side: 'a' | 'b', uuid: string) => void;
}) {
  return (
    <div className="conv-cmp-expand">
      {aUuid && (
        <Side side="a" uuid={aUuid} text={aText} onOpenInReader={onOpenInReader} />
      )}
      {bUuid && (
        <Side side="b" uuid={bUuid} text={bText} onOpenInReader={onOpenInReader} />
      )}
    </div>
  );
}

function Side({
  side,
  uuid,
  text,
  onOpenInReader,
}: {
  side: 'a' | 'b';
  uuid: string;
  text: string | undefined;
  onOpenInReader: (side: 'a' | 'b', uuid: string) => void;
}) {
  const sideLabel = side === 'a' ? 'A' : 'B';
  return (
    <div className={`conv-cmp-expand-side conv-cmp-expand-side--${side}`}>
      {/* #228 S5 E2 — a visible A/B chip (blue = A, purple = B) anchors which
          side this text belongs to once the column header has scrolled away. */}
      <span className={`conv-cmp-expand-chip conv-cmp-expand-chip--${side}`} aria-hidden="true">{sideLabel}</span>
      <div className="conv-cmp-expand-body">
        {text === undefined ? (
          <span className="conv-cmp-expand-loading">loading…</span>
        ) : (
          <pre className="conv-cmp-expand-text">{text}</pre>
        )}
      </div>
      <button
        type="button"
        className="conv-cmp-expand-open"
        aria-label={`Open in reader — run ${sideLabel} at this prompt`}
        onClick={() => onOpenInReader(side, uuid)}
      >
        open in reader →
      </button>
    </div>
  );
}
