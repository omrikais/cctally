import { highlightBody } from './CodeBlock';

// A Read result rendered with a separated line-number gutter. Stored
// `result.text` is `cat -n` form (`<number>\t<content>`, no leading padding;
// verified against real transcripts; parser-capped at 4000 chars). Strip the
// gutter, highlight the source as ONE block (multi-line tokens like docstrings
// stay correct), and render the numbers in a dim non-highlighted column. The
// gutter and code derive from ONE rows array, so their line counts match by
// construction (no off-by-one). No-gutter input degrades to the exact existing
// plain result <pre> (dim + 220px cap; never highlights an error message).

export interface GutterRow { num: string; content: string }

export function splitGutter(text: string): GutterRow[] {
  return text.split('\n').map((line) => {
    const m = /^\s*(\d+)\t(.*)$/.exec(line);
    return m ? { num: m[1], content: m[2] } : { num: '', content: line };
  });
}

export function LineNumberedCode({ code, lang }: { code: string; lang: string }) {
  const rows = splitGutter(code);
  if (!rows.some((r) => r.num)) {
    return <pre className="conv-code conv-code--result">{code}</pre>;
  }
  const source = rows.map((r) => r.content).join('\n');
  const gutter = rows.map((r) => r.num).join('\n');
  return (
    <div className="conv-code--numbered">
      <span className="cb-gutter" aria-hidden="true">{gutter}</span>
      <pre className="conv-code conv-code--hl">{highlightBody(source, lang)}</pre>
    </div>
  );
}
