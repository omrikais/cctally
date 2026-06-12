// Minimal ANSI SGR tokenizer for the conversation reader's terminal view (#177
// S3). Empirically only ~0.17% of real Bash results carry any escape codes, so
// a hand-rolled ~30-line SGR color handler covers them and everything else
// degrades to plain monospace — no ANSI library (spec §2 / §4.2). We handle
// SGR FOREGROUND COLOR + reset only; every other escape (cursor moves, clears,
// background, etc.) is stripped from the visible text. Pure + JSX-free for the
// tokenizer; `AnsiText` is the thin React wrapper that emits <span>s (never
// dangerouslySetInnerHTML, like highlightBody).

export interface AnsiSpan {
  text: string;
  cls: string | null;
}

// SGR foreground codes → a stable CSS class the terminal stylesheet colors.
// 30–37 standard, 90–97 bright. 90 (bright black) maps to a dim class.
const SGR_FG: Record<number, string> = {
  30: 'ansi-blk', 31: 'ansi-red', 32: 'ansi-grn', 33: 'ansi-yel',
  34: 'ansi-blu', 35: 'ansi-mag', 36: 'ansi-cyn', 37: 'ansi-wht',
  90: 'ansi-dim', 91: 'ansi-red', 92: 'ansi-grn', 93: 'ansi-yel',
  94: 'ansi-blu', 95: 'ansi-mag', 96: 'ansi-cyn', 97: 'ansi-wht',
};

const SGR = /\x1b\[([0-9;]*)m/g;
const OTHER_ESC = /\x1b\[[0-9;]*[A-Za-z]/g;

// Split `input` into color-tagged spans. A `\x1b[…m` SGR marker either resets
// (empty arg or code 0) or selects a foreground color; runs between markers
// carry the active class. Non-SGR CSI escapes are stripped from the visible
// text, and any span left empty by that strip is dropped (so we never emit a
// zero-length span the caller would render as a stray <span/>).
export function parseAnsi(input: string): AnsiSpan[] {
  const spans: AnsiSpan[] = [];
  let last = 0;
  let cls: string | null = null;
  let m: RegExpExecArray | null;
  SGR.lastIndex = 0;
  while ((m = SGR.exec(input)) !== null) {
    if (m.index > last) spans.push({ text: input.slice(last, m.index), cls });
    const codes = m[1].split(';').filter(Boolean).map(Number);
    // No-arg `\x1b[m` and an explicit `0` both reset to the default color.
    if (codes.length === 0 || codes.includes(0)) cls = null;
    for (const c of codes) if (SGR_FG[c]) cls = SGR_FG[c];
    last = SGR.lastIndex;
  }
  if (last < input.length) spans.push({ text: input.slice(last), cls });
  // Strip any leftover non-SGR escapes from the visible text, then drop the
  // spans that strip left empty.
  return spans
    .map((s) => ({ ...s, text: s.text.replace(OTHER_ESC, '') }))
    .filter((s) => s.text.length > 0);
}

// React wrapper: render the tokenized spans, coloring the ones with a class.
export function AnsiText({ text }: { text: string }) {
  return (
    <>
      {parseAnsi(text).map((s, i) =>
        s.cls ? (
          <span key={i} className={s.cls}>
            {s.text}
          </span>
        ) : (
          <span key={i}>{s.text}</span>
        ),
      )}
    </>
  );
}
