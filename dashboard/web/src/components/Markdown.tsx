import ReactMarkdown from 'react-markdown';
import type { ExtraProps } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ComponentPropsWithoutRef } from 'react';
import type { Element } from 'hast';
import { CodeBlock, isRegistered } from '../conversations/CodeBlock';

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

export function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: MdLink, pre: PreBlock }}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
