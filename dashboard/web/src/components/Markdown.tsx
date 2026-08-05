import ReactMarkdown from 'react-markdown';
import type { ExtraProps, Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useContext, useMemo } from 'react';
import type { ComponentPropsWithoutRef } from 'react';
import type { Element } from 'hast';
import { CodeBlock, isRegistered } from '../conversations/CodeBlock';
import {
  applyExactMarksPlugin,
  applyMarksPlugin,
  splitToReactNodes,
  useFindSplit,
  type ExactLeafTarget,
} from '../conversations/findMark';
import { ExactFindContext } from '../conversations/HighlightContext';

// Prose-first markdown for conversation messages. remark-gfm for
// tables/strikethrough/task-lists; NO rehype-raw, so raw HTML stays
// escaped (spec §4 security posture). Links open in a new tab with a
// safe rel. Fenced code blocks with a registered language render through
// CodeBlock (refractor → hast → React ELEMENTS — never an HTML string);
// no-language and unknown-language fences stay plain monospace <pre>.
// No dangerouslySetInnerHTML anywhere.
// #463 S5 (F20/F21) — ALLOWLIST, not a deny-list. The previous guard named four
// neutralized shapes; a bare relative target ("url", "docs/x.md") matched none of
// them and fetched against the dashboard origin, and the corpus holds 891 of
// them. A deny-list can only ever exclude the shapes someone thought of.
//
// mailto is admitted deliberately: react-markdown's own url transform already
// permits it, this app models mailto autolinks in findProjection.ts, and a
// mailto issues no request to our origin and discloses no filesystem path.
// #anchor is NOT admitted — the reader navigates via scrollIntoView, never via
// in-page anchors, and a content-supplied hash change would now fight the
// deep-link routing that owns the hash.
const SAFE_LINK = /^(?:https?:\/\/|mailto:)/i;
const SAFE_IMAGE = /^https?:\/\//i;

export function MdLink({ href, children, ...rest }: ComponentPropsWithoutRef<'a'>) {
  if (typeof href !== 'string' || !SAFE_LINK.test(href.trim())) {
    // The destination is deliberately dropped rather than moved to a title:
    // "discloses no path" is a property of what we serialize, and an attribute
    // is serialization. Authored child text is preserved as written. `rest` is
    // NOT spread here — it carries the hast `node` and any author-supplied
    // attributes, which is exactly how a destination would reach an attribute
    // despite the guard.
    return <span className="conv-inert-link">{children}</span>;
  }
  return (
    <a {...rest} href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

function MdImage({ src, alt: _alt, title: _title, ...rest }: ComponentPropsWithoutRef<'img'>) {
  if (typeof src !== 'string' || !SAFE_IMAGE.test(src.trim())) {
    return <span className="conv-local-media-unavailable" role="note">local screenshot unavailable</span>;
  }
  // The two branches of this file spread `rest` under opposite conventions, so
  // state why. MdLink withholds it on the branch that DROPS the destination,
  // where a spread is precisely how the dropped destination would reach an
  // attribute. Here there is no withheld destination: `src` has already passed
  // SAFE_IMAGE, and `src`, `alt` and `title` are destructured out of `rest`, so
  // no author-supplied duplicate can override the props written explicitly
  // below. Withholding `rest` on this branch would gain no privacy and would
  // drop the attributes react-markdown itself supplies.
  return <img {...rest} src={src} alt={_alt ?? ''} title={_title} loading="lazy" />;
}

// Pull the raw text out of a react-markdown <code> child's hast node. Its
// children are hast text nodes; concatenating their values yields the fence
// body verbatim (no markup), which CodeBlock then tokenizes.
function codeText(node: Element | undefined): string {
  const value = (children: Element['children']): string => children.map((child) => {
    if (child.type === 'text') return child.value;
    if (child.type === 'element') return value(child.children);
    return '';
  }).join('');
  return value(node?.children ?? []);
}

function codeExactTargets(node: Element | undefined): ExactLeafTarget[] {
  const targets: ExactLeafTarget[] = [];
  let cursor = 0;
  const walk = (children: Element['children']) => {
    for (const child of children) {
      if (child.type === 'text') {
        cursor += Array.from(child.value).length;
        continue;
      }
      if (child.type !== 'element') continue;
      const start = cursor;
      walk(child.children);
      if (child.tagName !== 'mark') continue;
      const occurrenceId = child.properties?.['data-find-occurrence-id'];
      const leafKey = child.properties?.['data-find-leaf-key'];
      if (typeof occurrenceId !== 'string' || typeof leafKey !== 'string') continue;
      targets.push({
        occurrenceId,
        leafKey,
        start,
        end: cursor,
        current: child.properties?.['data-find-current'] === 'true',
      });
    }
  };
  walk(node?.children ?? []);
  return targets;
}

// PRE-centric override (Codex P2): react-markdown v9 emits a fenced block as
// `pre > code`. Detecting the single <code> child here — rather than overriding
// `code` — avoids a <pre><pre> double-wrap AND keeps no-language fences. A
// fence with a registered `language-*` class renders CodeBlock; otherwise a
// plain <pre class="conv-code"> (inline `code` is untouched, so `` `x` `` stays
// a bare <code>).
function PreBlock({ node, children }: ComponentPropsWithoutRef<'pre'> & ExtraProps) {
  const split = useFindSplit();
  const codeEl = node?.children?.find((c): c is Element => c.type === 'element' && c.tagName === 'code');
  const classNameProp = codeEl?.properties?.className;
  const cls = Array.isArray(classNameProp) ? classNameProp.join(' ') : String(classNameProp ?? '');
  const lang = /language-(\w+)/.exec(cls)?.[1];
  const exactTargets = codeExactTargets(codeEl);
  if (lang && isRegistered(lang)) {
    return (
      <CodeBlock
        lang={lang}
        filename={undefined}
        code={codeText(codeEl)}
        exactTargets={exactTargets}
      />
    );
  }
  // #236 — unregistered / no-language fence: highlight-aware. `children` is the
  // prose-walk-SKIPPED rehype tree, so without this an unknown-language fence
  // never marks. Preserve the <code> wrapper; only swap content when find is on.
  const raw = codeText(codeEl);
  return (
    <pre className="conv-code">
      {split && raw ? <code>{splitToReactNodes(raw, split)}</code> : children}
    </pre>
  );
}

export function Markdown({
  children,
  components,
  findBlockKey,
  findSurface = 'body',
  findLeafPrefix,
}: {
  children: string;
  components?: Components;
  findBlockKey?: string;
  findSurface?: 'body' | 'call' | 'output' | 'completion';
  findLeafPrefix?: string;
}) {
  const split = useFindSplit();
  const exact = useContext(ExactFindContext);
  const exactTargets = useMemo(() => {
    if (!exact || !findBlockKey) return [];
    const targets: ExactLeafTarget[] = [];
    for (const occurrence of exact.occurrences) {
      if (occurrence.container_block_key !== findBlockKey || occurrence.surface !== findSurface) continue;
      for (const fragment of occurrence.fragments) {
        if (findLeafPrefix && !fragment.leaf_key.startsWith(`${findLeafPrefix}/`)) continue;
        const leafKey = findLeafPrefix
          ? fragment.leaf_key.slice(findLeafPrefix.length + 1)
          : fragment.leaf_key;
        targets.push({
        occurrenceId: occurrence.occurrence_id,
        leafKey,
        ...(findLeafPrefix ? { sourceLeafKey: fragment.leaf_key } : {}),
        start: fragment.start,
        end: fragment.end,
        current: occurrence.occurrence_id === exact.selectedOccurrenceId,
        });
      }
    }
    return targets;
  }, [exact, findBlockKey, findSurface, findLeafPrefix]);
  const rehypePlugins = useMemo(
    () => exactTargets.length
      ? [[applyExactMarksPlugin, children, exactTargets] as [
          typeof applyExactMarksPlugin, string, ExactLeafTarget[],
        ]]
      : split ? [[applyMarksPlugin, split] as [typeof applyMarksPlugin, typeof split]] : [],
    [children, exactTargets, split],
  );
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={rehypePlugins}
        components={{ a: MdLink, img: MdImage, pre: PreBlock, ...components }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
