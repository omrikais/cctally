// #177 S5 §4 — jump-to-next cursor math, shared by the reader's e/u/b/p keys and
// the OutlinePanel glyph cluster. Pure: given a SORTED ascending list of target
// turn indices (outline-skeleton space), the cursor's current turn index, and a
// direction, return the next/previous target index strictly past the cursor — or
// null when there is none (no wrap). A cursor of -1 means "before the start" so a
// forward jump finds the first target.
export function nextTarget(indices: number[], cursor: number, dir: 1 | -1): number | null {
  if (dir === 1) {
    for (const i of indices) if (i > cursor) return i;
    return null;
  }
  // dir === -1: scan from the end for the first index strictly less than cursor.
  for (let k = indices.length - 1; k >= 0; k--) {
    if (indices[k] < cursor) return indices[k];
  }
  return null;
}
