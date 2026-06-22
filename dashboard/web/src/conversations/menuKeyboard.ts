// #224 — pure roving-focus index math for the ExportMenu / FocusMoreMenu APG
// menu keyboard pattern. Given a vertical-navigation key, the current active
// index, and the item count, return the next active index (ArrowDown/Up wrap;
// Home/End jump to the ends), or null when the key is not one the menu handles
// or there are no items. The component owns the imperative `.focus()`; this
// keeps the index arithmetic testable in isolation.
export function nextRovingIndex(
  key: string,
  current: number,
  count: number,
): number | null {
  if (count <= 0) return null;
  switch (key) {
    case 'ArrowDown':
      return (current + 1) % count;
    case 'ArrowUp':
      return (current - 1 + count) % count;
    case 'Home':
      return 0;
    case 'End':
      return count - 1;
    default:
      return null;
  }
}
