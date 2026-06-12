import { createContext } from 'react';

// #177 S6 — find-term highlight terms for the Markdown renderer. The reader
// provides the find bar's DEBOUNCED needle (whitespace-split, empties dropped)
// while find is open; `null` (the default) means "no highlighting" — the
// Markdown renderer then adds NO rehype plugin (zero overhead on the common
// path). Memoize the provider value so the memoized message items only
// re-render when the joined terms actually change.
export const HighlightContext = createContext<string[] | null>(null);
