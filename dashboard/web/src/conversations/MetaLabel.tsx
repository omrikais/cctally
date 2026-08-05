// #463 S5 Task 1 — the single owner of meta-row label and sections-value
// presentation. Both elements wrap at whitespace on narrow viewports (see the
// `#root .conv-meta > summary` reset in index.css); they are deliberately NOT
// ellipsized, because a `title` on a non-focusable span cannot be invoked by a
// sighted touch user, and truncation without a reachable disclosure is the
// defect this session exists to remove, not a fix for it.
export function MetaLabel({ text }: { text: string }): JSX.Element {
  return <span className="conv-meta-label">{text}</span>;
}

export function MetaName({ text }: { text: string }): JSX.Element {
  return <span className="conv-meta-name">· {text}</span>;
}
