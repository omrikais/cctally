// #299 — pure roving-focus decision for the Sessions grid (row-primary,
// "grid-lite"). Mirrors conversations/menuKeyboard.ts: the pure decision lives
// here; the component owns the imperative .focus()/scrollIntoView. Given the
// pressed key and the current focus context — on the row (`onRow`) vs on the
// `cellIdx`-th of `cellCount` VISIBLE controls — return the navigation action,
// or null when the key is not one the grid handles (let it bubble / default).
// Row moves are RELATIVE intents ('prev'/'next'/'first'/'last'); the component
// resolves + clamps them against the rendered rows (no wrap). Enter/Space on a
// cell returns null so the focused button's native activation fires.

export type RovingAction =
  | { kind: 'row'; to: 'prev' | 'next' | 'first' | 'last' }
  | { kind: 'cell'; to: number }
  | { kind: 'rowFocus' }
  | { kind: 'activateRow' }
  | null;

export interface RovingContext {
  /** True when the <tr> itself is focused (row mode); false when a control is. */
  onRow: boolean;
  /** Index of the focused control among the row's visible controls; -1 on the row. */
  cellIdx: number;
  /** Count of visible controls in the active row. */
  cellCount: number;
}

export function rovingAction(key: string, ctx: RovingContext): RovingAction {
  const { onRow, cellIdx, cellCount } = ctx;
  switch (key) {
    case 'ArrowDown':
      return { kind: 'row', to: 'next' };
    case 'ArrowUp':
      return { kind: 'row', to: 'prev' };
    case 'Home':
      return { kind: 'row', to: 'first' };
    case 'End':
      return { kind: 'row', to: 'last' };
    case 'ArrowRight':
      if (onRow) return cellCount > 0 ? { kind: 'cell', to: 0 } : null;
      return cellIdx < cellCount - 1 ? { kind: 'cell', to: cellIdx + 1 } : null;
    case 'ArrowLeft':
      if (onRow) return null;
      return cellIdx > 0 ? { kind: 'cell', to: cellIdx - 1 } : { kind: 'rowFocus' };
    case 'Enter':
    case ' ':
      return onRow ? { kind: 'activateRow' } : null;
    default:
      return null;
  }
}
