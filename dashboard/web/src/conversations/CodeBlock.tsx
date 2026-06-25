import { refractor } from 'refractor/core';
import tsx from 'refractor/tsx';
import typescript from 'refractor/typescript';
import javascript from 'refractor/javascript';
import jsx from 'refractor/jsx';
import json from 'refractor/json';
import bash from 'refractor/bash';
import python from 'refractor/python';
import css from 'refractor/css';
import diff from 'refractor/diff';
import markdown from 'refractor/markdown';
import yaml from 'refractor/yaml';
import { toJsxRuntime } from 'hast-util-to-jsx-runtime';
import { Fragment, jsx as _jsx, jsxs as _jsxs } from 'react/jsx-runtime';
import type { Root } from 'hast';
import { CopyButton } from './CopyButton';
import { applyMarksToHast, splitToReactNodes, useFindSplit, type SplitFn } from './findMark';

// Syntax-highlighted fenced-code renderer (C6). refractor (the Prism
// tokenizer) produces a hast tree; hast-util-to-jsx-runtime turns that tree
// into React ELEMENTS — never an HTML string — so the Markdown security
// posture (no rehype-raw, no dangerouslySetInnerHTML) is fully preserved.
// A tight registered-language set keeps the bundle small; an unknown language
// (or a refractor highlight failure) degrades to plain monospace text.
//
// NOTE: refractor v5 exposes each grammar via the `refractor/<lang>` subpath
// (the v4 `refractor/lang/<lang>.js` form is gone); the registered grammar set
// is unchanged.
[typescript, tsx, javascript, jsx, json, bash, python, css, diff, markdown, yaml].forEach((l) =>
  refractor.register(l),
);

// Short fence-info aliases → canonical registered grammar names.
const ALIASES: Record<string, string> = {
  ts: 'typescript',
  js: 'javascript',
  sh: 'bash',
  shell: 'bash',
  yml: 'yaml',
  py: 'python',
};

export function isRegistered(lang: string): boolean {
  const l = ALIASES[lang] ?? lang;
  return refractor.registered(l);
}

// Shared refractor→React-elements primitive. refractor (Prism) → hast →
// React ELEMENTS (never an HTML string), so the no-rehype-raw /
// no-dangerouslySetInnerHTML posture holds. Unknown language or a refractor
// throw degrades to the raw string. Used by CodeBlock (prose) AND the tool
// I/O panels (REQUEST json, Read RESULT source) — one chokepoint.
//
// #236 — optional `split` makes the chokepoint find-highlight-aware. Absent or
// null → byte-identical to before (no walk, no string split). With a split:
// registered language runs the refractor hast through applyMarksToHast
// (skipCode: false — marks ARE wanted inside a code block) before toJsxRuntime;
// an unregistered language (or a refractor throw) routes through
// splitToReactNodes so the raw text still marks.
export function highlightBody(code: string, lang: string, split?: SplitFn | null): React.ReactNode {
  const l = ALIASES[lang] ?? lang;
  if (refractor.registered(l)) {
    try {
      const tree = refractor.highlight(code, l) as unknown as Root;
      if (split) applyMarksToHast(tree, split, { skipCode: false });
      return toJsxRuntime(tree, { Fragment, jsx: _jsx, jsxs: _jsxs });
    } catch {
      return split ? splitToReactNodes(code, split) : code;
    }
  }
  return split ? splitToReactNodes(code, split) : code;
}

export function CodeBlock({ lang, filename, code }: { lang: string; filename?: string; code: string }) {
  const split = useFindSplit();
  return (
    <div className="codeblock">
      <div className="cb-head">
        <span className="cb-lang">{lang}</span>
        {filename && <span className="cb-file">{filename}</span>}
        <CopyButton text={code} className="cb-copy" />
      </div>
      <pre className="conv-code conv-code--hl">{highlightBody(code, lang, split)}</pre>
    </div>
  );
}
