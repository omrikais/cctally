import ReactMarkdown from 'react-markdown';
import type { ExtraProps } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useContext, useMemo } from 'react';
import type { ComponentPropsWithoutRef } from 'react';
import type { Element, Root, RootContent, Text } from 'hast';
import { CodeBlock, isRegistered } from '../conversations/CodeBlock';
import { HighlightContext } from '../conversations/HighlightContext';

// Prose-first markdown for conversation messages. remark-gfm for
// tables/strikethrough/task-lists; NO rehype-raw, so raw HTML stays
// escaped (spec §4 security posture). Links open in a new tab with a
// safe rel. Fenced code blocks with a registered language render through
// CodeBlock (refractor → hast → React ELEMENTS — never an HTML string);
// no-language and unknown-language fences stay plain monospace <pre>.
// No dangerouslySetInnerHTML anywhere.
function MdLink({ href, children, ...rest }: ComponentPropsWithoutRef<'a'>) {
  return (
    <a {...rest} href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

// Pull the raw text out of a react-markdown <code> child's hast node. Its
// children are hast text nodes; concatenating their values yields the fence
// body verbatim (no markup), which CodeBlock then tokenizes.
function codeText(node: Element | undefined): string {
  return (node?.children ?? []).map((c) => (c.type === 'text' ? c.value : '')).join('');
}

// PRE-centric override (Codex P2): react-markdown v9 emits a fenced block as
// `pre > code`. Detecting the single <code> child here — rather than overriding
// `code` — avoids a <pre><pre> double-wrap AND keeps no-language fences. A
// fence with a registered `language-*` class renders CodeBlock; otherwise a
// plain <pre class="conv-code"> (inline `code` is untouched, so `` `x` `` stays
// a bare <code>).
function PreBlock({ node, children }: ComponentPropsWithoutRef<'pre'> & ExtraProps) {
  const codeEl = node?.children?.find((c): c is Element => c.type === 'element' && c.tagName === 'code');
  const classNameProp = codeEl?.properties?.className;
  const cls = Array.isArray(classNameProp) ? classNameProp.join(' ') : String(classNameProp ?? '');
  const lang = /language-(\w+)/.exec(cls)?.[1];
  if (lang && isRegistered(lang)) {
    return <CodeBlock lang={lang} filename={undefined} code={codeText(codeEl)} />;
  }
  return <pre className="conv-code">{children}</pre>;
}

// #177 S6 — split a text-node value on case-insensitive term matches, yielding
// alternating literal/hit segments. Longest terms first so a multi-term query
// like ["cache", "cache.db"] prefers the longer overlap. Returns null when the
// value contains no match (the walker then leaves the node untouched — no churn).
function splitByTerms(value: string, terms: string[]): { s: string; hit: boolean }[] | null {
  const lowerVal = value.toLowerCase();
  const lowered = [...terms].map((t) => t.toLowerCase()).sort((a, b) => b.length - a.length);
  const out: { s: string; hit: boolean }[] = [];
  let i = 0;
  let any = false;
  while (i < value.length) {
    let matched = 0;
    for (const t of lowered) {
      if (t && lowerVal.startsWith(t, i)) { matched = t.length; break; }
    }
    if (matched > 0) {
      out.push({ s: value.slice(i, i + matched), hit: true });
      i += matched;
      any = true;
    } else {
      // Accumulate a literal run until the next match start (or end).
      const last = out[out.length - 1];
      if (last && !last.hit) last.s += value[i];
      else out.push({ s: value[i], hit: false });
      i += 1;
    }
  }
  return any ? out : null;
}

// #177 S6 — inline rehype plugin (no new dependency). Walks the hast tree
// wrapping case-insensitive term matches in <mark>, skipping `code`/`pre`
// subtrees (Q5 boundary: no marks in code blocks / inline code / refractor
// token streams). The plugin is only added to the pipeline when terms are
// non-empty (HighlightContext provides them), so the common path pays nothing.
function rehypeMarkTerms(terms: string[]) {
  const SKIP = new Set(['code', 'pre']);
  const walkChildren = (children: RootContent[], inSkip: boolean): RootContent[] => {
    const next: RootContent[] = [];
    for (const child of children) {
      if (child.type === 'element') {
        const childSkip = inSkip || SKIP.has(child.tagName);
        child.children = walkChildren(child.children, childSkip) as Element['children'];
        next.push(child);
      } else if (child.type === 'text' && !inSkip && child.value) {
        const parts = splitByTerms(child.value, terms);
        if (parts) {
          for (const p of parts) {
            if (p.hit) {
              next.push({
                type: 'element', tagName: 'mark', properties: {},
                children: [{ type: 'text', value: p.s } as Text],
              } as Element);
            } else {
              next.push({ type: 'text', value: p.s } as Text);
            }
          }
        } else {
          next.push(child);
        }
      } else {
        next.push(child);
      }
    }
    return next;
  };
  return (tree: Root) => {
    tree.children = walkChildren(tree.children, false) as Root['children'];
  };
}

export function Markdown({ children }: { children: string }) {
  const highlightTerms = useContext(HighlightContext);
  // Memoize the rehype-plugin array on the JOINED terms so the renderer (and
  // every memoized message item below it) only re-runs the walk when the
  // debounced needle actually changes. Null / empty terms → no plugin (the
  // zero-overhead passthrough).
  const termsKey = highlightTerms && highlightTerms.length ? highlightTerms.join(' ') : '';
  const rehypePlugins = useMemo(
    () => (termsKey ? [[rehypeMarkTerms, termsKey.split(' ')] as [typeof rehypeMarkTerms, string[]]] : []),
    [termsKey],
  );
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={rehypePlugins}
        components={{ a: MdLink, pre: PreBlock }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
