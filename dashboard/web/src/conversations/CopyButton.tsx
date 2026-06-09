import { useCopy } from './useCopy';
import { CopyIcon, CheckIcon } from './ConvIcons';

// Compact, icon-only copy button (G2 §5b). The clipboard glyph swaps to a
// check while `copied`. aria-label carries the state (Copy → Copied) since the
// glyph is icon-only. onClick stops propagation so a copy click never toggles
// an enclosing <details>.
export function CopyButton({ text, className }: { text: string; className?: string }) {
  const { copied, copy } = useCopy();
  return (
    <button
      type="button"
      className={`conv-copy-btn ${className ?? ''}`.trim()}
      aria-label={copied ? 'Copied' : 'Copy'}
      onClick={(e) => {
        e.stopPropagation();
        copy(text);
      }}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
    </button>
  );
}
