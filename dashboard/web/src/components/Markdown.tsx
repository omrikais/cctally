import ReactMarkdown from 'react-markdown';
import type { ExtraProps } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useContext, useMemo } from 'react';
import type { ComponentPropsWithoutRef } from 'react';
import type { Element, Root, RootContent, Text } from 'hast';
import { CodeBlock, isRegistered } from '../conversations/CodeBlock';
import { HighlightContext } from '../conversations/HighlightContext';

// #223 — mirror the server find-scan caps in bin/_lib_conversation_query.py
// (_FIND_REGEX_MAX_LEN / _FIND_SCAN_TEXT_CAP). Best-effort ReDoS/perf guards for
// the per-text-node regex walk; TS can't import the Python constants.
const FIND_REGEX_MAX_LEN = 1000;
const FIND_SCAN_TEXT_CAP = 200_000;

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

// #177 S6 / #217 S4 — split a text-node value on term matches, yielding
// alternating literal/hit segments. Longest terms first so a multi-term query
// like ["cache", "cache.db"] prefers the longer overlap. Case-INsensitive by
// default; `caseSensitive` (the find bar's `Aa` toggle) makes the comparison
// exact-case so the inline underline tracks the find results. Returns null when
// the value contains no match (the walker then leaves the node untouched).
function splitByTerms(value: string, terms: string[], caseSensitive: boolean): { s: string; hit: boolean }[] | null {
  const cmpVal = caseSensitive ? value : value.toLowerCase();
  const cmpTerms = [...terms]
    .map((t) => (caseSensitive ? t : t.toLowerCase()))
    .sort((a, b) => b.length - a.length);
  const out: { s: string; hit: boolean }[] = [];
  let i = 0;
  let any = false;
  while (i < value.length) {
    let matched = 0;
    for (const t of cmpTerms) {
      if (t && cmpVal.startsWith(t, i)) { matched = t.length; break; }
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

// #223 — regex variant of splitByTerms. Applies a GLOBAL regex per text node,
// yielding alternating literal/hit segments. Zero-width matches (e.g. `x*`,
// lookaheads) advance lastIndex by one and emit no mark, so the loop terminates
// and no empty <mark> is produced. A node longer than FIND_SCAN_TEXT_CAP is
// left untouched (returns null). Returns null when no non-empty match.
function splitByRegex(value: string, re: RegExp): { s: string; hit: boolean }[] | null {
  if (value.length > FIND_SCAN_TEXT_CAP) return null;
  re.lastIndex = 0;
  const out: { s: string; hit: boolean }[] = [];
  let last = 0;
  let any = false;
  let m: RegExpExecArray | null;
  while ((m = re.exec(value)) !== null) {
    if (m[0].length === 0) { re.lastIndex += 1; continue; }
    if (m.index > last) out.push({ s: value.slice(last, m.index), hit: false });
    out.push({ s: m[0], hit: true });
    last = m.index + m[0].length;
    any = true;
  }
  if (!any) return null;
  if (last < value.length) out.push({ s: value.slice(last), hit: false });
  return out;
}

// #177 S6 / #223 — shared rehype tree-walk. Wraps a per-text-node split fn's
// `hit` segments in <mark>, skipping `code`/`pre` subtrees (no marks in code
// blocks / inline code). Added to the pipeline only when there is something to
// match, so the common path pays nothing.
function makeMarkPlugin(split: (value: string) => { s: string; hit: boolean }[] | null) {
  const SKIP = new Set(['code', 'pre']);
  const walkChildren = (children: RootContent[], inSkip: boolean): RootContent[] => {
    const next: RootContent[] = [];
    for (const child of children) {
      if (child.type === 'element') {
        const childSkip = inSkip || SKIP.has(child.tagName);
        child.children = walkChildren(child.children, childSkip) as Element['children'];
        next.push(child);
      } else if (child.type === 'text' && !inSkip && child.value) {
        const parts = split(child.value);
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

function rehypeMarkTerms(terms: string[], caseSensitive: boolean) {
  return makeMarkPlugin((value) => splitByTerms(value, terms, caseSensitive));
}

// #223 — best-effort regex highlight. An over-cap source or an invalid pattern
// becomes a no-op transformer (no marks, no throw) — the server already 400s an
// invalid regex and the find bar shows the alert.
function rehypeMarkRegex(source: string, caseSensitive: boolean) {
  if (source.length > FIND_REGEX_MAX_LEN) return (_tree: Root) => {};
  let re: RegExp;
  try {
    re = new RegExp(source, caseSensitive ? 'g' : 'gi');
  } catch {
    return (_tree: Root) => {};
  }
  return makeMarkPlugin((value) => splitByRegex(value, re));
}

export function Markdown({ children }: { children: string }) {
  const highlight = useContext(HighlightContext);
  // Primitive memo keys so the walk (and every memoized message item) re-runs
  // only when the matcher actually changes.
  const kind = highlight?.kind ?? null;
  const caseSensitive = highlight?.caseSensitive ?? false;
  const key = highlight
    ? (highlight.kind === 'terms' ? highlight.terms.join(' ') : highlight.source)
    : '';
  const rehypePlugins = useMemo(() => {
    if (!key) return [];
    if (kind === 'terms') {
      return [[rehypeMarkTerms, key.split(' '), caseSensitive] as [typeof rehypeMarkTerms, string[], boolean]];
    }
    return [[rehypeMarkRegex, key, caseSensitive] as [typeof rehypeMarkRegex, string, boolean]];
  }, [kind, key, caseSensitive]);
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
