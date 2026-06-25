// #228 S5 E5 — resolve the comparison pick-mode banner label: the anchor
// session's cached title (truncated) when known, else the opaque short hash
// (cold-boot / pasted-URL case, before the rail title cache is populated).
const MAX = 48;
export function pickBannerLabel(
  anchor: string,
  titles: Record<string, string>,
): { kind: 'title' | 'hash'; text: string } {
  const t = titles[anchor]?.trim();
  if (t) return { kind: 'title', text: t.length > MAX ? `${t.slice(0, MAX - 1)}…` : t };
  return { kind: 'hash', text: anchor.slice(0, 8) };
}
