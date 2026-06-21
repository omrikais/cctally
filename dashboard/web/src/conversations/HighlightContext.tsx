import { createContext } from 'react';

// #177 S6 / #217 S4 — find-term highlight context for the Markdown renderer.
// The reader provides the find bar's DEBOUNCED needle (whitespace-split,
// empties dropped) plus the case-sensitive toggle while find is open; `null`
// (the default) means "no highlighting" — the Markdown renderer then adds NO
// rehype plugin (zero overhead on the common path). In regex mode the reader
// passes `null` so the inline underline is suppressed (decision b). Memoize the
// provider value so the memoized message items only re-render when the joined
// terms (or the case flag) actually change.
export interface HighlightTerms {
  terms: string[];
  caseSensitive: boolean;
}

export const HighlightContext = createContext<HighlightTerms | null>(null);
