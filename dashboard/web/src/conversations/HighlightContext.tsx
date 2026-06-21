import { createContext } from 'react';

// #177 S6 / #217 S4 / #223 — find-term highlight context for the Markdown
// renderer. The reader provides either a DEBOUNCED term list (whitespace-split,
// empties dropped) or a regex SOURCE, plus the case-sensitive toggle, while find
// is open; `null` (the default) means "no highlighting" — the renderer adds NO
// rehype plugin (zero overhead on the common path). Regex highlighting is
// best-effort per-text-node (#223 supersedes S4 decision b). Memoize the
// provider value so memoized message items re-render only when the joined terms
// / source (or the case flag, or the kind) actually change.
export type HighlightTerms =
  | { kind: 'terms'; terms: string[]; caseSensitive: boolean }
  | { kind: 'regex'; source: string; caseSensitive: boolean };

export const HighlightContext = createContext<HighlightTerms | null>(null);
