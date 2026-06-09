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
import { CopyButton } from './CopyButton';

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

export function CodeBlock({ lang, filename, code }: { lang: string; filename?: string; code: string }) {
  const l = ALIASES[lang] ?? lang;
  // refractor.highlight can throw on a grammar edge case; never let a code
  // block crash the reader — fall back to the raw text on any failure.
  let body: React.ReactNode = code;
  if (refractor.registered(l)) {
    try {
      const tree = refractor.highlight(code, l);
      body = toJsxRuntime(tree, { Fragment, jsx: _jsx, jsxs: _jsxs });
    } catch {
      body = code;
    }
  }
  return (
    <div className="codeblock">
      <div className="cb-head">
        <span className="cb-lang">{lang}</span>
        {filename && <span className="cb-file">{filename}</span>}
        <CopyButton text={code} className="cb-copy" />
      </div>
      <pre className="conv-code conv-code--hl">{body}</pre>
    </div>
  );
}
